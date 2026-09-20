"""Campaign metrics, and side-by-side comparison of campaign variants.

Everything here is derived from three tables — ``AgentRun`` (what the machine
did and what it cost), ``Message`` (what left the building) and ``Prospect``
(where people got to) — with grouped aggregates only. No counters are
maintained anywhere, because a denormalised counter that drifts from the
underlying rows is worse than no metric at all: it is a number people trust.

The unit economics are the point. "Reply rate" tells you whether the copy
works; ``cost_per_qualified_lead`` and ``cost_per_meeting`` tell you whether
the whole thing is worth running, and they are the numbers that make a variant
experiment decidable.
"""
from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import (
    FUNNEL_STAGES,
    AgentRun,
    Approval,
    Campaign,
    ConversationThread,
    Message,
    Prospect,
)
from ..serializers import iso
from .agents_cfg import agent_stats
from .deps import SessionDep, UserDep, load_campaign
from .errors import invalid_request, not_found

router = APIRouter(tags=["metrics"])

SENT_STATUSES = ("sent", "delivered", "replied")


def _rate(numerator: int, denominator: int) -> Optional[float]:
    """``None`` when there is nothing to divide by.

    Reporting 0% for a campaign that has not sent anything yet is a lie that
    looks like a failure; ``None`` renders as "—" and is honest.
    """
    return round(numerator / denominator, 4) if denominator else None


def _cost(total: float, denominator: int) -> Optional[float]:
    return round(total / denominator, 4) if denominator else None


async def metrics_block(session: AsyncSession, campaign: Campaign) -> dict:
    """The full metric block for one campaign. Fixed query count."""
    campaign_id = campaign.id

    # --- funnel ----------------------------------------------------------
    funnel = {stage: 0 for stage in FUNNEL_STAGES}
    rows = await session.execute(
        select(Prospect.stage, func.count(Prospect.id))
        .where(Prospect.campaign_id == campaign_id, Prospect.status != "rejected")
        .group_by(Prospect.stage)
    )
    for stage, count in rows:
        if stage in funnel:
            funnel[stage] = int(count or 0)

    status_rows = await session.execute(
        select(Prospect.status, func.count(Prospect.id))
        .where(Prospect.campaign_id == campaign_id)
        .group_by(Prospect.status)
    )
    by_status = {status: int(count or 0) for status, count in status_rows}

    total_prospects = sum(by_status.values())
    active_prospects = total_prospects - by_status.get("rejected", 0)
    qualified = int(
        (
            await session.execute(
                select(func.count(Prospect.id)).where(
                    Prospect.campaign_id == campaign_id, Prospect.fit_verdict == "qualified"
                )
            )
        ).scalar()
        or 0
    )
    meetings = int(
        (
            await session.execute(
                select(func.count(Prospect.id)).where(
                    Prospect.campaign_id == campaign_id,
                    or_(Prospect.stage == "meeting", Prospect.status == "meeting_booked"),
                )
            )
        ).scalar()
        or 0
    )

    # --- channels --------------------------------------------------------
    channels: dict[str, dict] = {}

    def channel_slot(name: str) -> dict:
        return channels.setdefault(name, {"sent": 0, "replied": 0, "meetings": 0})

    rows = await session.execute(
        select(Message.channel, func.count(func.distinct(Message.prospect_id)))
        .where(
            Message.campaign_id == campaign_id,
            Message.direction == "outbound",
            Message.status.in_(SENT_STATUSES),
        )
        .group_by(Message.channel)
    )
    for channel, count in rows:
        channel_slot(channel)["sent"] = int(count or 0)

    rows = await session.execute(
        select(Message.channel, func.count(func.distinct(Message.prospect_id)))
        .where(Message.campaign_id == campaign_id, Message.direction == "inbound")
        .group_by(Message.channel)
    )
    for channel, count in rows:
        channel_slot(channel)["replied"] = int(count or 0)

    rows = await session.execute(
        select(ConversationThread.channel, func.count(ConversationThread.id))
        .where(
            ConversationThread.campaign_id == campaign_id,
            ConversationThread.status == "meeting_booked",
        )
        .group_by(ConversationThread.channel)
    )
    for channel, count in rows:
        channel_slot(channel)["meetings"] = int(count or 0)

    for slot in channels.values():
        slot["reply_rate"] = _rate(slot["replied"], slot["sent"])

    # --- messages and replies -------------------------------------------
    message_rows = await session.execute(
        select(Message.direction, Message.status, func.count(Message.id))
        .where(Message.campaign_id == campaign_id)
        .group_by(Message.direction, Message.status)
    )
    messages_sent = 0
    messages_blocked = 0
    messages_failed = 0
    messages_pending_approval = 0
    inbound_messages = 0
    for direction, status, count in message_rows:
        count = int(count or 0)
        if direction == "inbound":
            inbound_messages += count
            continue
        if status in SENT_STATUSES:
            messages_sent += count
        elif status == "blocked":
            messages_blocked += count
        elif status == "failed":
            messages_failed += count
        elif status == "pending_approval":
            messages_pending_approval += count

    contacted = sum(slot["sent"] for slot in channels.values())
    contacted = int(
        (
            await session.execute(
                select(func.count(func.distinct(Message.prospect_id))).where(
                    Message.campaign_id == campaign_id,
                    Message.direction == "outbound",
                    Message.status.in_(SENT_STATUSES),
                )
            )
        ).scalar()
        or 0
    )
    replied = int(
        (
            await session.execute(
                select(func.count(func.distinct(Message.prospect_id))).where(
                    Message.campaign_id == campaign_id, Message.direction == "inbound"
                )
            )
        ).scalar()
        or 0
    )

    sentiment_rows = await session.execute(
        select(ConversationThread.sentiment, func.count(ConversationThread.id))
        .where(ConversationThread.campaign_id == campaign_id)
        .group_by(ConversationThread.sentiment)
    )
    sentiments = {(s or "unknown"): int(c or 0) for s, c in sentiment_rows}
    positive = sentiments.get("positive", 0)

    # --- agents and cost -------------------------------------------------
    per_agent = await agent_stats(session, campaign_id)
    total_cost = round(sum(a["total_cost_usd"] for a in per_agent.values()), 6)
    total_runs = sum(a["runs"] for a in per_agent.values())
    tokens_in = sum(a["tokens_in"] for a in per_agent.values())
    tokens_out = sum(a["tokens_out"] for a in per_agent.values())
    failed_runs = sum(a["failed"] for a in per_agent.values())

    pending_approvals = int(
        (
            await session.execute(
                select(func.count(Approval.id)).where(
                    Approval.campaign_id == campaign_id, Approval.status == "pending"
                )
            )
        ).scalar()
        or 0
    )

    return {
        "campaign_id": campaign_id,
        "campaign_name": campaign.name,
        "status": campaign.status,
        "variant_of": campaign.variant_of,
        "duplicated_from": campaign.duplicated_from,
        "created_at": iso(campaign.created_at),
        "funnel": funnel,
        "prospects": {
            "total": total_prospects,
            "active": active_prospects,
            "qualified": qualified,
            "contacted": contacted,
            "replied": replied,
            "meetings": meetings,
            "by_status": by_status,
        },
        "channels": channels,
        "messages": {
            "sent": messages_sent,
            "inbound": inbound_messages,
            "blocked": messages_blocked,
            "failed": messages_failed,
            "pending_approval": messages_pending_approval,
        },
        "rates": {
            "qualification_rate": _rate(qualified, total_prospects),
            "reply_rate": _rate(replied, contacted),
            "positive_reply_rate": _rate(positive, replied),
            "meeting_rate": _rate(meetings, contacted),
            "conversion_rate": _rate(meetings, total_prospects),
        },
        "replies": {
            "positive": positive,
            "neutral": sentiments.get("neutral", 0),
            "negative": sentiments.get("negative", 0),
        },
        "cost": {
            "total_usd": total_cost,
            "runs": total_runs,
            "failed_runs": failed_runs,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            # The three numbers that decide whether this campaign is worth
            # running, in increasing order of what they actually prove.
            "cost_per_prospect": _cost(total_cost, total_prospects),
            "cost_per_qualified_lead": _cost(total_cost, qualified),
            "cost_per_meeting": _cost(total_cost, meetings),
        },
        "agents": per_agent,
        "pending_approvals": pending_approvals,
    }


@router.get("/campaigns/{campaign_id}/metrics")
async def campaign_metrics(campaign_id: str, session: SessionDep, _: UserDep) -> dict:
    """Everything measurable about one campaign."""
    campaign = await load_campaign(session, campaign_id)
    return await metrics_block(session, campaign)


@router.get("/metrics/compare")
async def compare(
    session: SessionDep,
    _: UserDep,
    ids: Annotated[str, Query(description="Comma-separated campaign ids, 2 to 6")],
) -> dict:
    """The same metric block for several campaigns, side by side.

    This is what a duplicate is *for*: two campaigns that started from
    identical prompts, one of which was then changed, compared on the numbers
    that matter. Order follows the ids given so the UI can keep its columns
    stable across refreshes.
    """
    wanted = [part.strip() for part in (ids or "").split(",") if part.strip()]
    if len(wanted) < 2:
        raise invalid_request("Pick at least two campaigns to compare.")
    if len(wanted) > 6:
        raise invalid_request("Compare at most six campaigns at a time.")

    found = {
        campaign.id: campaign
        for campaign in (
            await session.execute(select(Campaign).where(Campaign.id.in_(wanted)))
        ).scalars()
    }
    missing = [campaign_id for campaign_id in wanted if campaign_id not in found]
    if missing:
        raise not_found("campaign", missing[0])

    return {"items": [await metrics_block(session, found[cid]) for cid in wanted]}


__all__ = ["router", "metrics_block"]
