"""Campaign lifecycle: the control plane's front door.

Everything a manager can do to a campaign without opening it lives here —
list, summarise, create, edit, and move it through its lifecycle. Two rules
shape the whole module:

* **The server owns the state machine.** The UI shows the buttons it thinks
  apply, but every transition is re-checked here against the table in
  ``TRANSITIONS``. A colleague who paused the campaign three seconds ago wins,
  and the loser gets a 409 that says so in plain English.
* **List endpoints never loop.** Campaign metrics are four grouped aggregates
  over the whole set, not three queries per row. A campaigns page that grows
  linearly with the number of campaigns is the first thing to fall over in a
  demo, and it is entirely avoidable.
"""
from __future__ import annotations

import datetime as dt
from typing import Annotated, Any, Literal, Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit
from ..agents.schemas import CAMPAIGN_SYSTEM_PROMPT, default_prompt
from ..channels.registry import SUPPORTED_CHANNELS
from ..db import new_id
from ..models import (
    AGENT_KEYS,
    FUNNEL_STAGES,
    AgentConfig,
    AgentRun,
    Approval,
    Campaign,
    CampaignChannel,
    CampaignRep,
    ConversationThread,
    Message,
    PromptVersion,
    Prospect,
    User,
    utcnow,
)
from ..orchestrator.engine import discover_prospects, tick_campaign
from ..serializers import serialize_campaign, serialize_campaign_detail, user_ref
from .deps import SessionDep, UserDep, actor, load_campaign, require_choice, steps_json
from .errors import ApiError, invalid_request, invalid_state, invalid_transition, not_found

router = APIRouter(tags=["campaigns"])

# The campaign-level system prompt is stored as a prompt version like any
# other, under a reserved agent key, so prompt history is one table.
CAMPAIGN_PROMPT_KEY = "__campaign__"

CHANNEL_STATES = ("active", "paused", "off")
SENT_STATUSES = ("sent", "delivered", "replied")

ChannelType = Literal["linkedin", "email", "voice", "sms", "whatsapp"]
ChannelState = Literal["active", "paused", "off"]


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------
class ChannelSpec(BaseModel):
    type: ChannelType
    state: ChannelState = "active"
    daily_limit: int = Field(default=50, ge=0, le=10_000)
    config: dict[str, Any] = Field(default_factory=dict)


class CampaignCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    label: Optional[str] = Field(default=None, max_length=64)
    description: Optional[str] = None
    icp_summary: Optional[str] = Field(default=None, max_length=400)
    icp: dict[str, Any] = Field(default_factory=dict)
    product_context: dict[str, Any] = Field(default_factory=dict)
    objective: Optional[str] = None
    policies: dict[str, Any] = Field(default_factory=dict)
    channels: list[ChannelSpec] = Field(default_factory=list)
    owner_id: Optional[str] = None


class CampaignUpdate(BaseModel):
    """Every field optional: PATCH is a partial update, and an absent key must
    be distinguishable from an explicit null."""

    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    label: Optional[str] = Field(default=None, max_length=64)
    description: Optional[str] = None
    icp_summary: Optional[str] = Field(default=None, max_length=400)
    icp: Optional[dict[str, Any]] = None
    product_context: Optional[dict[str, Any]] = None
    objective: Optional[str] = None
    policies: Optional[dict[str, Any]] = None
    owner_id: Optional[str] = None


class DiscoverRequest(BaseModel):
    count: int = Field(default=5, ge=1, le=50)


# ---------------------------------------------------------------------------
# Aggregates (the anti-N+1 layer)
# ---------------------------------------------------------------------------
async def metrics_for(session: AsyncSession, campaign_ids: list[str]) -> dict[str, dict]:
    """``{campaign_id: {prospects, outreach, meetings}}`` in three queries.

    Definitions are fixed here and reused by the detail page and the metrics
    router, because the README promises the campaigns list and the campaign
    dashboard agree on what "outreach" means.
    """
    empty = {"prospects": 0, "outreach": 0, "meetings": 0}
    if not campaign_ids:
        return {}
    result: dict[str, dict] = {cid: dict(empty) for cid in campaign_ids}

    # Prospects: everything still in play. A rejected prospect was never a
    # prospect as far as the funnel is concerned.
    rows = await session.execute(
        select(Prospect.campaign_id, func.count(Prospect.id))
        .where(Prospect.campaign_id.in_(campaign_ids), Prospect.status != "rejected")
        .group_by(Prospect.campaign_id)
    )
    for campaign_id, count in rows:
        result[campaign_id]["prospects"] = int(count or 0)

    # Outreach: distinct people actually contacted, not messages sent. Three
    # follow-ups to one prospect is one prospect contacted.
    rows = await session.execute(
        select(Message.campaign_id, func.count(func.distinct(Message.prospect_id)))
        .where(
            Message.campaign_id.in_(campaign_ids),
            Message.direction == "outbound",
            Message.status.in_(SENT_STATUSES),
        )
        .group_by(Message.campaign_id)
    )
    for campaign_id, count in rows:
        result[campaign_id]["outreach"] = int(count or 0)

    rows = await session.execute(
        select(Prospect.campaign_id, func.count(Prospect.id))
        .where(
            Prospect.campaign_id.in_(campaign_ids),
            or_(Prospect.stage == "meeting", Prospect.status == "meeting_booked"),
        )
        .group_by(Prospect.campaign_id)
    )
    for campaign_id, count in rows:
        result[campaign_id]["meetings"] = int(count or 0)

    return result


async def pending_approvals_for(session: AsyncSession, campaign_ids: list[str]) -> dict[str, int]:
    if not campaign_ids:
        return {}
    counts = {cid: 0 for cid in campaign_ids}
    rows = await session.execute(
        select(Approval.campaign_id, func.count(Approval.id))
        .where(Approval.campaign_id.in_(campaign_ids), Approval.status == "pending")
        .group_by(Approval.campaign_id)
    )
    for campaign_id, count in rows:
        counts[campaign_id] = int(count or 0)
    return counts


async def as_list_item(session: AsyncSession, campaign: Campaign) -> dict:
    """One campaign in the shape the list returns, which is also what every
    lifecycle action returns so the UI can swap a row in place."""
    metrics = (await metrics_for(session, [campaign.id])).get(campaign.id)
    pending = (await pending_approvals_for(session, [campaign.id])).get(campaign.id, 0)
    return serialize_campaign(campaign, metrics, pending)


# ---------------------------------------------------------------------------
# Seeding: what makes a brand-new campaign runnable
# ---------------------------------------------------------------------------
async def seed_campaign_defaults(
    session: AsyncSession, campaign: Campaign, created_by: Optional[str] = None
) -> None:
    """Give a new campaign its v1 prompts and one config row per agent.

    Without this a fresh campaign would activate and then immediately stall,
    because ``agents/base.py`` resolves the *active* prompt version and there
    would not be one. Seeding at creation is what makes "create, activate, run"
    a three-click demo.
    """
    session.add(
        PromptVersion(
            id=new_id("pv"),
            campaign_id=campaign.id,
            agent_key=CAMPAIGN_PROMPT_KEY,
            version=1,
            content=CAMPAIGN_SYSTEM_PROMPT,
            notes="Seeded with the platform default campaign system prompt.",
            is_active=True,
            created_by_id=created_by,
            activated_at=utcnow(),
        )
    )
    for agent_key in AGENT_KEYS:
        session.add(
            PromptVersion(
                id=new_id("pv"),
                campaign_id=campaign.id,
                agent_key=agent_key,
                version=1,
                content=default_prompt(agent_key),
                notes="Seeded with the platform default prompt.",
                is_active=True,
                created_by_id=created_by,
                activated_at=utcnow(),
            )
        )
        session.add(
            AgentConfig(
                id=new_id("acfg"),
                campaign_id=campaign.id,
                agent_key=agent_key,
                enabled=True,
                state="active",
            )
        )
    await session.flush()


# ---------------------------------------------------------------------------
# List and summary
# ---------------------------------------------------------------------------
@router.get("/campaigns")
async def list_campaigns(session: SessionDep, _: UserDep) -> dict:
    """Every campaign, archived included.

    The README is explicit that filtering, search and sort happen in the
    browser, so this returns the whole set in creation order and does not
    accept filters it would have to keep in sync with the client.
    """
    campaigns = (
        await session.execute(select(Campaign).order_by(Campaign.created_at.asc()))
    ).scalars().all()

    ids = [c.id for c in campaigns]
    metrics = await metrics_for(session, ids)
    pending = await pending_approvals_for(session, ids)

    return {
        "items": [
            serialize_campaign(c, metrics.get(c.id), pending.get(c.id, 0)) for c in campaigns
        ]
    }


@router.get("/campaigns/summary")
async def campaign_summary(session: SessionDep, _: UserDep) -> dict:
    """The strip across the top of the campaigns page.

    Declared before ``/campaigns/{campaign_id}`` on purpose: FastAPI matches in
    declaration order, and "summary" would otherwise be read as a campaign id.
    """
    now = utcnow()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    thirty_days = now - dt.timedelta(days=30)
    seven_days = now - dt.timedelta(days=7)
    fourteen_days = now - dt.timedelta(days=14)

    live_ids = (
        await session.execute(select(Campaign.id).where(Campaign.status == "live"))
    ).scalars().all()

    live_campaigns = len(live_ids)
    total_campaigns = int(
        (
            await session.execute(
                select(func.count(Campaign.id)).where(Campaign.status != "archived")
            )
        ).scalar()
        or 0
    )

    in_motion = 0
    in_motion_today = 0
    if live_ids:
        # "In motion" is the honest count: a prospect the system is still
        # working on. Rejected and closed are finished, whichever way it went.
        moving = (
            Prospect.campaign_id.in_(live_ids),
            Prospect.status.notin_(["rejected", "closed"]),
        )
        in_motion = int(
            (await session.execute(select(func.count(Prospect.id)).where(*moving))).scalar() or 0
        )
        in_motion_today = int(
            (
                await session.execute(
                    select(func.count(Prospect.id)).where(*moving, Prospect.created_at >= day_start)
                )
            ).scalar()
            or 0
        )

    meeting_clause = or_(Prospect.stage == "meeting", Prospect.status == "meeting_booked")

    async def meetings_between(start: dt.datetime, end: Optional[dt.datetime] = None) -> int:
        clauses = [meeting_clause, Prospect.updated_at >= start]
        if end is not None:
            clauses.append(Prospect.updated_at < end)
        return int(
            (await session.execute(select(func.count(Prospect.id)).where(*clauses))).scalar() or 0
        )

    meetings_30d = await meetings_between(thirty_days)
    this_week = await meetings_between(seven_days)
    last_week = await meetings_between(fourteen_days, seven_days)

    pending_approvals = int(
        (
            await session.execute(
                select(func.count(Approval.id)).where(Approval.status == "pending")
            )
        ).scalar()
        or 0
    )

    return {
        "live_campaigns": live_campaigns,
        "total_campaigns": total_campaigns,
        "prospects_in_motion": in_motion,
        "prospects_in_motion_delta_today": in_motion_today,
        "meetings_30d": meetings_30d,
        # Signed: a bad week should show as a negative number, not be hidden.
        "meetings_30d_delta_wow": this_week - last_week,
        "pending_approvals": pending_approvals,
    }


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------
@router.post("/campaigns", status_code=201)
async def create_campaign(body: CampaignCreate, session: SessionDep, user: UserDep) -> dict:
    """Create a campaign as a draft, fully seeded and ready to activate."""
    owner: Optional[User] = None
    if body.owner_id:
        owner = await session.get(User, body.owner_id)
        if owner is None:
            raise not_found("representative", body.owner_id)

    campaign = Campaign(
        id=new_id("cmp"),
        name=body.name.strip(),
        label=body.label,
        description=body.description,
        icp_summary=body.icp_summary,
        status="draft",
        owner_id=owner.id if owner else None,
        icp=body.icp,
        product_context=body.product_context,
        objective=body.objective,
    )
    campaign.owner = owner
    campaign.paused_by = None
    campaign.channels = [
        CampaignChannel(
            id=new_id("chn"),
            campaign_id=campaign.id,
            type=spec.type,
            state=spec.state,
            daily_limit=spec.daily_limit,
            config=spec.config,
        )
        for spec in body.channels
    ]
    session.add(campaign)
    await session.flush()

    if body.policies:
        # Merged *after* the flush has applied the column default, so a partial
        # policy block overrides individual keys instead of wiping the
        # guardrail defaults the orchestrator relies on.
        campaign.policies = {**(campaign.policies or {}), **body.policies}
        await session.flush()

    await seed_campaign_defaults(session, campaign, created_by=user.id)
    await audit.record_campaign_event(
        session,
        campaign,
        event_type="campaign_created",
        severity="success",
        message=f"created by {actor(user)} as a draft.",
        actor=actor(user),
        payload={"channels": [c.type for c in campaign.channels]},
    )
    await session.commit()
    return await as_list_item(session, campaign)


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------
@router.get("/campaigns/{campaign_id}")
async def get_campaign(campaign_id: str, session: SessionDep, _: UserDep) -> dict:
    """The campaign dashboard: funnel, agent activity, outcomes and reps.

    Every block is a grouped aggregate, so the page costs a fixed number of
    queries no matter how many prospects the campaign holds.
    """
    campaign = await load_campaign(session, campaign_id)

    funnel = {stage: 0 for stage in FUNNEL_STAGES}
    rows = await session.execute(
        select(Prospect.stage, func.count(Prospect.id))
        .where(Prospect.campaign_id == campaign_id, Prospect.status != "rejected")
        .group_by(Prospect.stage)
    )
    for stage, count in rows:
        if stage in funnel:
            funnel[stage] = int(count or 0)
    funnel["rejected"] = int(
        (
            await session.execute(
                select(func.count(Prospect.id)).where(
                    Prospect.campaign_id == campaign_id, Prospect.status == "rejected"
                )
            )
        ).scalar()
        or 0
    )

    # --- agent activity --------------------------------------------------
    runs_by_status: dict[str, int] = {"succeeded": 0, "failed": 0, "skipped": 0, "running": 0, "pending": 0}
    rows = await session.execute(
        select(AgentRun.status, func.count(AgentRun.id), func.coalesce(func.sum(AgentRun.cost_usd), 0.0))
        .where(AgentRun.campaign_id == campaign_id)
        .group_by(AgentRun.status)
    )
    total_cost = 0.0
    for status, count, cost in rows:
        runs_by_status[status] = int(count or 0)
        total_cost += float(cost or 0.0)

    approval_rows = await session.execute(
        select(Approval.status, func.count(Approval.id))
        .where(Approval.campaign_id == campaign_id)
        .group_by(Approval.status)
    )
    approvals_by_status = {status: int(count or 0) for status, count in approval_rows}
    pending_approvals = approvals_by_status.get("pending", 0)

    agent_activity = {
        "runs": sum(runs_by_status.values()),
        "by_status": runs_by_status,
        "failed": runs_by_status.get("failed", 0),
        "pending_approvals": pending_approvals,
        # Every escalation raises an approval, so the approval table is the
        # complete record of "the agent stopped and asked a human".
        "escalations": sum(approvals_by_status.values()),
        "approvals_by_status": approvals_by_status,
        "total_cost_usd": round(total_cost, 4),
    }

    # --- outcomes --------------------------------------------------------
    sentiment_rows = await session.execute(
        select(ConversationThread.sentiment, func.count(ConversationThread.id))
        .where(ConversationThread.campaign_id == campaign_id)
        .group_by(ConversationThread.sentiment)
    )
    sentiments = {(s or "unknown"): int(c or 0) for s, c in sentiment_rows}

    metrics = (await metrics_for(session, [campaign_id])).get(campaign_id) or {
        "prospects": 0,
        "outreach": 0,
        "meetings": 0,
    }
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

    def rate(numerator: int, denominator: int) -> Optional[float]:
        """None rather than 0.0 when there is no denominator: "no data yet"
        and "0%" mean very different things to a manager."""
        return round(numerator / denominator, 4) if denominator else None

    outcomes = {
        "positive_replies": sentiments.get("positive", 0),
        "negative_replies": sentiments.get("negative", 0),
        "neutral_replies": sentiments.get("neutral", 0),
        "replied_prospects": replied,
        "meetings": metrics["meetings"],
        "reply_rate": rate(replied, metrics["outreach"]),
        "positive_reply_rate": rate(sentiments.get("positive", 0), replied),
        "meeting_rate": rate(metrics["meetings"], metrics["outreach"]),
        "conversion_rate": rate(metrics["meetings"], metrics["prospects"]),
    }

    # --- reps ------------------------------------------------------------
    rep_rows = (
        await session.execute(select(CampaignRep).where(CampaignRep.campaign_id == campaign_id))
    ).scalars().all()
    reps = [
        {
            **(user_ref(rep.user) or {}),
            "role": rep.user.role if rep.user else None,
            "initials": rep.user.initials if rep.user else None,
            "is_sending_identity": rep.is_sending_identity,
        }
        for rep in rep_rows
    ]

    return serialize_campaign_detail(
        campaign,
        metrics=metrics,
        funnel=funnel,
        pending_approvals=pending_approvals,
        agent_activity=agent_activity,
        outcomes=outcomes,
        reps=reps,
    )


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------
@router.patch("/campaigns/{campaign_id}")
async def update_campaign(
    campaign_id: str,
    body: CampaignUpdate,
    session: SessionDep,
    user: UserDep,
    force: Annotated[bool, Query()] = False,
) -> dict:
    """Edit a campaign's configuration.

    Changing the ICP of a *live* campaign is refused unless ``?force=true``:
    prospects already scored against the old ICP stay in the funnel, so the
    campaign would silently be running two definitions of "qualified" at once.
    The override exists because sometimes that is exactly what the manager
    wants — so it is allowed, loudly, and written to the audit trail.
    """
    campaign = await load_campaign(session, campaign_id)
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        return await as_list_item(session, campaign)

    icp_fields = {"icp", "icp_summary"} & set(changes)
    if icp_fields and campaign.status == "live" and not force:
        raise invalid_state(
            "This campaign is live, so changing its ICP would re-score prospects mid-flight. "
            "Pause it first, or confirm the change to apply it anyway."
        )

    if "owner_id" in changes:
        owner_id = changes.pop("owner_id")
        owner = await session.get(User, owner_id) if owner_id else None
        if owner_id and owner is None:
            raise not_found("representative", owner_id)
        campaign.owner_id = owner.id if owner else None
        campaign.owner = owner

    for field, value in changes.items():
        setattr(campaign, field, value)
    campaign.updated_at = utcnow()

    forced_note = " (forced while live)" if icp_fields and campaign.status == "live" else ""
    await audit.record_campaign_event(
        session,
        campaign,
        event_type="campaign_updated",
        severity="warning" if forced_note else "info",
        message=f"configuration updated by {actor(user)}{forced_note}: {', '.join(sorted(changes)) or 'owner'}.",
        actor=actor(user),
        payload={"fields": sorted(changes), "forced": bool(forced_note)},
    )
    await session.commit()
    return await as_list_item(session, campaign)


# ---------------------------------------------------------------------------
# Execution (declared before the /{action} catch-all so they are not shadowed)
# ---------------------------------------------------------------------------
@router.post("/campaigns/{campaign_id}/run")
async def run_campaign(campaign_id: str, session: SessionDep, user: UserDep) -> dict:
    """Tick this campaign once, by hand.

    The scheduler does this on a timer; this endpoint is how a demo makes the
    autonomous layer visible on command. The guardrails are identical either
    way — a paused campaign or an engaged kill switch returns the same blocked
    results here as it would at 3am.
    """
    campaign = await load_campaign(session, campaign_id)
    results = await tick_campaign(session, campaign)
    await audit.record_campaign_event(
        session,
        campaign,
        event_type="campaign_ticked",
        message=f"run manually by {actor(user)}: {len(results)} action(s).",
        actor=actor(user),
        payload={"actions": [r.action for r in results]},
    )
    await session.commit()
    return {"results": steps_json(results)}


@router.post("/campaigns/{campaign_id}/discover")
async def discover(
    campaign_id: str, body: DiscoverRequest, session: SessionDep, user: UserDep
) -> dict:
    """Top the campaign up with fresh prospects on demand."""
    campaign = await load_campaign(session, campaign_id)
    results = await discover_prospects(session, campaign, count=body.count)
    await audit.record_campaign_event(
        session,
        campaign,
        event_type="discovery_requested",
        message=f"discovery run by {actor(user)}: {len(results)} prospect(s) added.",
        actor=actor(user),
        payload={"requested": body.count, "added": len(results)},
    )
    await session.commit()
    return {"results": steps_json(results)}


@router.post("/campaigns/{campaign_id}/channels/{channel_type}/{state}")
async def set_channel_state(
    campaign_id: str, channel_type: str, state: str, session: SessionDep, user: UserDep
) -> dict:
    """Turn one channel on, pause it, or switch it off for one campaign.

    This is the narrowest of the four operational controls (channel → agent →
    campaign → platform kill switch), and the one a manager reaches for most:
    "stop the SMS, keep the email running".
    """
    campaign = await load_campaign(session, campaign_id)
    require_choice(channel_type, SUPPORTED_CHANNELS, "channel")
    require_choice(state, CHANNEL_STATES, "channel state")

    row = (
        await session.execute(
            select(CampaignChannel).where(
                CampaignChannel.campaign_id == campaign_id, CampaignChannel.type == channel_type
            )
        )
    ).scalar_one_or_none()

    previous = row.state if row else "not configured"
    if row is None:
        row = CampaignChannel(
            id=new_id("chn"), campaign_id=campaign_id, type=channel_type, state=state
        )
        session.add(row)
    else:
        row.state = state
    await session.flush()

    await audit.record_campaign_event(
        session,
        campaign,
        event_type="channel_state_changed",
        severity="warning" if state != "active" else "success",
        message=f"{channel_type} channel set to {state} by {actor(user)} (was {previous}).",
        actor=actor(user),
        payload={"channel": channel_type, "state": state, "previous": previous},
    )
    await session.commit()
    # Re-read so the serialized channel list reflects the change.
    await session.refresh(campaign, ["channels"])
    return await as_list_item(session, campaign)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
#: action -> (allowed source statuses, resulting status). ``None`` for the
#: result means the action does not simply set a status (duplicate).
TRANSITIONS: dict[str, tuple[set[str], Optional[str]]] = {
    "pause": ({"live"}, "paused"),
    "resume": ({"paused"}, "live"),
    "complete": ({"live", "paused"}, "completed"),
    "archive": ({"draft", "live", "paused", "completed"}, "archived"),
    "activate": ({"draft"}, "live"),
    "duplicate": ({"draft", "live", "paused", "completed", "archived"}, None),
}

_REFUSALS = {
    "pause": "Only a live campaign can be paused. This one is {status}.",
    "resume": "Only a paused campaign can be resumed. This one is {status}.",
    "complete": "Only a live or paused campaign can be completed. This one is {status}.",
    "archive": "This campaign is already archived.",
    "activate": "Only a draft campaign can be activated. This one is {status}.",
}


async def _validate_activation(session: AsyncSession, campaign: Campaign) -> None:
    """Refuse to go live half-configured.

    A campaign that activates without a channel, an ICP or a prompt does not
    fail loudly — it runs and produces nothing, which is far harder to debug
    than a 422 naming the missing piece.
    """
    missing: list[str] = []

    active_channel = (
        await session.execute(
            select(CampaignChannel.id).where(
                CampaignChannel.campaign_id == campaign.id, CampaignChannel.state == "active"
            )
        )
    ).scalars().first()
    if not active_channel:
        missing.append("at least one active channel")

    if not (campaign.icp or campaign.icp_summary):
        missing.append("an ideal customer profile")

    active_prompt = (
        await session.execute(
            select(PromptVersion.id).where(
                PromptVersion.campaign_id == campaign.id, PromptVersion.is_active.is_(True)
            )
        )
    ).scalars().first()
    if not active_prompt:
        missing.append("an active prompt version")

    if missing:
        raise ApiError(
            422,
            "incomplete_campaign",
            "This campaign is not ready to go live. It still needs " + _join(missing) + ".",
        )


def _join(items: list[str]) -> str:
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


async def _duplicate(session: AsyncSession, source: Campaign, user: User) -> Campaign:
    """Deep-copy a campaign into a new draft.

    Channels, agent configs *and* prompt versions are copied, so a variant
    experiment starts from behaviour identical to its parent and any difference
    in results is attributable to the change the manager then makes. Metrics
    are not copied: the copy has run nothing.
    """
    copy = Campaign(
        id=new_id("cmp"),
        name=f"{source.name} (copy)",
        label=source.label,
        description=source.description,
        icp_summary=source.icp_summary,
        status="draft",
        owner_id=source.owner_id,
        icp=dict(source.icp or {}),
        product_context=dict(source.product_context or {}),
        objective=source.objective,
        policies=dict(source.policies or {}),
        duplicated_from=source.id,
        # Variants of variants group under the original, which is what makes
        # /metrics/compare?ids=... a meaningful A/B view.
        variant_of=source.variant_of or source.id,
    )
    copy.owner = source.owner
    copy.paused_by = None
    copy.channels = [
        CampaignChannel(
            id=new_id("chn"),
            campaign_id=copy.id,
            type=channel.type,
            state=channel.state,
            daily_limit=channel.daily_limit,
            config=dict(channel.config or {}),
        )
        for channel in source.channels
    ]
    session.add(copy)
    await session.flush()

    configs = (
        await session.execute(select(AgentConfig).where(AgentConfig.campaign_id == source.id))
    ).scalars().all()
    for config in configs:
        session.add(
            AgentConfig(
                id=new_id("acfg"),
                campaign_id=copy.id,
                agent_key=config.agent_key,
                enabled=config.enabled,
                state=config.state,
                model=config.model,
                tools=list(config.tools or []),
                thresholds=dict(config.thresholds or {}),
                escalation_rules=dict(config.escalation_rules or {}),
                dronahq_agent_id=config.dronahq_agent_id,
            )
        )

    versions = (
        await session.execute(
            select(PromptVersion)
            .where(PromptVersion.campaign_id == source.id)
            .order_by(PromptVersion.version.asc())
        )
    ).scalars().all()
    for version in versions:
        session.add(
            PromptVersion(
                id=new_id("pv"),
                campaign_id=copy.id,
                agent_key=version.agent_key,
                version=version.version,
                content=version.content,
                notes=version.notes,
                is_active=version.is_active,
                created_by_id=user.id,
                activated_at=version.activated_at,
            )
        )

    if not configs or not versions:
        # A source campaign created before seeding existed still yields a
        # runnable copy rather than a dead one.
        await seed_campaign_defaults(session, copy, created_by=user.id)

    await session.flush()
    return copy


@router.post("/campaigns/{campaign_id}/{action}")
async def campaign_action(
    campaign_id: str, action: str, session: SessionDep, user: UserDep
) -> dict:
    """Move a campaign through its lifecycle.

    ``action`` is one of pause, resume, complete, archive, activate or
    duplicate. The response is always a campaign in **list-item shape** — the
    updated one, or for ``duplicate`` the new draft — so the UI can replace a
    row without a second fetch.
    """
    require_choice(action, TRANSITIONS.keys(), "campaign action")
    campaign = await load_campaign(session, campaign_id)

    allowed, target = TRANSITIONS[action]
    if campaign.status not in allowed:
        raise invalid_transition(
            _REFUSALS.get(action, "That action is not available for a {status} campaign.").format(
                status=campaign.status
            )
        )

    if action == "duplicate":
        copy = await _duplicate(session, campaign, user)
        await audit.record_campaign_event(
            session,
            copy,
            event_type="campaign_duplicated",
            severity="success",
            message=f"created by {actor(user)} as a copy of {campaign.name}, including its prompts.",
            actor=actor(user),
            payload={"source_campaign_id": campaign.id},
        )
        await session.commit()
        return await as_list_item(session, copy)

    if action == "activate":
        await _validate_activation(session, campaign)

    previous = campaign.status
    campaign.status = target  # type: ignore[assignment]
    campaign.updated_at = utcnow()

    if action == "pause":
        campaign.paused_at = utcnow()
        campaign.paused_by_id = user.id
        campaign.paused_by = user
    elif action in {"resume", "activate"}:
        campaign.paused_at = None
        campaign.paused_by_id = None
        campaign.paused_by = None

    messages = {
        "pause": f"paused by {actor(user)}. All autonomous execution stopped.",
        "resume": f"resumed by {actor(user)}. Autonomous execution has restarted.",
        "complete": f"marked complete by {actor(user)}. No further outreach will be sent.",
        "archive": f"archived by {actor(user)}.",
        "activate": f"activated by {actor(user)}. The campaign is now live.",
    }
    severities = {
        "pause": "warning",
        "resume": "success",
        "complete": "info",
        "archive": "info",
        "activate": "success",
    }

    await audit.record_campaign_event(
        session,
        campaign,
        event_type=f"campaign_{action}d" if action != "pause" else "campaign_paused",
        severity=severities[action],
        message=messages[action],
        actor=actor(user),
        payload={"from": previous, "to": campaign.status},
    )
    await session.commit()
    return await as_list_item(session, campaign)


__all__ = ["router", "metrics_for", "pending_approvals_for", "seed_campaign_defaults"]
