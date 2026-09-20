"""Prospects: the list, and the "why did the agent do that?" view.

``GET /prospects/{id}`` is the most important read endpoint in the platform.
It returns the prospect *plus* the signals that justified contacting them, the
threads and messages that resulted, and every agent run behind those decisions
— with the model, cost, latency, the prompt version that was active and the
knowledge chunks that were retrieved. That is the difference between a system
that made a decision and a system that can show its working.
"""
from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from .. import audit
from ..db import new_id
from ..models import (
    AgentRun,
    Campaign,
    Company,
    ConversationThread,
    Message,
    Prospect,
    Signal,
    SuppressionEntry,
    utcnow,
)
from ..orchestrator.engine import advance_prospect
from ..serializers import iso, serialize_message, serialize_prospect, serialize_thread
from .deps import (
    SessionDep,
    UserDep,
    actor,
    clamp,
    load_campaign,
    load_prospect,
    step_json,
)
from .errors import not_found

router = APIRouter(tags=["prospects"])


class SuppressRequest(BaseModel):
    reason: str = Field(default="manually suppressed", max_length=120)
    scope: str = Field(default="global", pattern="^(global|campaign)$")


def serialize_signal(signal: Signal) -> dict:
    """Signals carry their source URL because personalisation is only allowed
    to cite what can be traced back to one."""
    return {
        "id": signal.id,
        "signal_type": signal.signal_type,
        "summary": signal.summary,
        "source_url": signal.source_url,
        "observed_at": iso(signal.observed_at),
        "confidence": signal.confidence,
        "created_at": iso(signal.created_at),
    }


def serialize_agent_run(run: AgentRun) -> dict:
    """One agent invocation, with everything needed to audit it: which prompt
    version ran, what it retrieved, what it cost and how long it took."""
    return {
        "id": run.id,
        "agent_key": run.agent_key,
        "status": run.status,
        "executor": run.executor,
        "model": run.model,
        "prompt_version_id": run.prompt_version_id,
        "retrieved_chunk_ids": run.retrieved_chunk_ids,
        "tokens_in": run.tokens_in,
        "tokens_out": run.tokens_out,
        "cost_usd": round(run.cost_usd or 0.0, 6),
        "latency_ms": run.latency_ms,
        "error": run.error,
        "output": run.output,
        "started_at": iso(run.started_at),
        "finished_at": iso(run.finished_at),
    }


@router.get("/campaigns/{campaign_id}/prospects")
async def list_prospects(
    campaign_id: str,
    session: SessionDep,
    _: UserDep,
    stage: Annotated[Optional[str], Query()] = None,
    status: Annotated[Optional[str], Query()] = None,
    q: Annotated[Optional[str], Query()] = None,
    limit: Annotated[Optional[int], Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    """Filtered, paged prospects for one campaign.

    ``total`` is the count for the filter, not the page, so the UI can show
    "50 of 1,284" without a second request.
    """
    await load_campaign(session, campaign_id)

    clauses = [Prospect.campaign_id == campaign_id]
    if stage:
        clauses.append(Prospect.stage == stage)
    if status:
        clauses.append(Prospect.status == status)

    # Search spans the person and their company, which is how a manager
    # actually remembers a prospect ("the one at Acme").
    joined = False
    if q:
        needle = f"%{q.strip().lower()}%"
        joined = True
        clauses.append(
            or_(
                func.lower(Prospect.full_name).like(needle),
                func.lower(Prospect.email).like(needle),
                func.lower(Prospect.designation).like(needle),
                func.lower(Company.name).like(needle),
            )
        )

    count_stmt = select(func.count(Prospect.id)).select_from(Prospect)
    list_stmt = select(Prospect)
    if joined:
        count_stmt = count_stmt.outerjoin(Company, Company.id == Prospect.company_id)
        list_stmt = list_stmt.outerjoin(Company, Company.id == Prospect.company_id)

    total = int((await session.execute(count_stmt.where(*clauses))).scalar() or 0)
    prospects = (
        await session.execute(
            list_stmt.where(*clauses)
            .order_by(Prospect.created_at.desc())
            .offset(offset)
            .limit(clamp(limit, 50, 200))
        )
    ).scalars().all()

    def with_flat_fields(p: Prospect) -> dict:
        item = serialize_prospect(p)
        # Frontend convenience fields: the prospects table renders these flat
        # keys directly rather than reaching into `company` or renaming
        # `fit_score`/`designation`.
        item["name"] = p.full_name
        item["title"] = p.designation
        item["company"] = p.company.name if p.company else None
        item["score"] = p.fit_score
        item["last_activity"] = iso(p.last_contacted_at or p.updated_at or p.created_at)
        return item

    return {
        "items": [with_flat_fields(p) for p in prospects],
        "total": total,
        "limit": clamp(limit, 50, 200),
        "offset": offset,
    }


@router.get("/prospects/{prospect_id}")
async def get_prospect(prospect_id: str, session: SessionDep, _: UserDep) -> dict:
    """Everything the platform knows and did about one person.

    Five bounded queries, one per collection — the page has a fixed cost no
    matter how long the conversation ran.
    """
    prospect = await load_prospect(session, prospect_id)

    signals = (
        await session.execute(
            select(Signal)
            .where(Signal.prospect_id == prospect_id)
            .order_by(Signal.confidence.desc(), Signal.observed_at.desc())
        )
    ).scalars().all()

    threads = (
        await session.execute(
            select(ConversationThread)
            .where(ConversationThread.prospect_id == prospect_id)
            .order_by(ConversationThread.last_activity_at.desc())
        )
    ).scalars().all()

    messages = (
        await session.execute(
            select(Message)
            .where(Message.prospect_id == prospect_id)
            .order_by(Message.created_at.asc())
        )
    ).scalars().all()

    runs = (
        await session.execute(
            select(AgentRun)
            .where(AgentRun.prospect_id == prospect_id)
            .order_by(AgentRun.started_at.asc())
        )
    ).scalars().all()

    payload = serialize_prospect(prospect)
    payload.update(
        {
            "signals": [serialize_signal(s) for s in signals],
            "threads": [serialize_thread(t) for t in threads],
            "messages": [serialize_message(m) for m in messages],
            "agent_runs": [serialize_agent_run(r) for r in runs],
            "cost_usd": round(sum(r.cost_usd or 0.0 for r in runs), 6),
        }
    )
    return payload


@router.post("/prospects/{prospect_id}/advance")
async def advance(prospect_id: str, session: SessionDep, user: UserDep) -> dict:
    """Push one prospect one step down the pipeline, by hand.

    Which step depends on where they are: research, qualify, or decide and
    send. The guardrails are the same as on a scheduled tick, so this cannot be
    used to get around a pause — it will simply return a blocked result saying
    so, which is exactly what a demo wants to show.
    """
    prospect = await load_prospect(session, prospect_id)
    campaign = await session.get(Campaign, prospect.campaign_id)
    if campaign is None:
        raise not_found("campaign", prospect.campaign_id)

    result = await advance_prospect(session, campaign, prospect)
    await audit.record(
        session,
        entity_type="prospect",
        entity_id=prospect.id,
        event_type="prospect_advanced",
        message=f"{prospect.full_name} advanced manually by {actor(user)}: {result.action}.",
        campaign_id=campaign.id,
        campaign_name=campaign.name,
        actor=actor(user),
        payload={"action": result.action, "detail": result.detail},
    )
    await session.commit()
    return step_json(result)


@router.post("/prospects/{prospect_id}/suppress")
async def suppress(
    prospect_id: str, body: SuppressRequest, session: SessionDep, user: UserDep
) -> dict:
    """Do not contact this person again — here or anywhere else.

    Every identifier we hold is written to the suppression list so the block
    survives the prospect being re-discovered under a different campaign, and
    the prospect itself is closed with their threads, so nothing queued is left
    waiting to fire.
    """
    prospect = await load_prospect(session, prospect_id)
    campaign = await session.get(Campaign, prospect.campaign_id)

    entry = SuppressionEntry(
        id=new_id("sup"),
        email=prospect.email,
        phone=prospect.phone,
        linkedin_url=prospect.linkedin_url,
        reason=body.reason,
        scope=body.scope,
        campaign_id=prospect.campaign_id if body.scope == "campaign" else None,
    )
    session.add(entry)

    prospect.status = "closed"
    prospect.next_action_at = None
    threads = (
        await session.execute(
            select(ConversationThread).where(ConversationThread.prospect_id == prospect_id)
        )
    ).scalars().all()
    for thread in threads:
        thread.status = "closed"
        thread.last_activity_at = utcnow()
    await session.flush()

    await audit.record(
        session,
        entity_type="prospect",
        entity_id=prospect.id,
        event_type="prospect_suppressed",
        severity="warning",
        message=(
            f"{prospect.full_name} added to the do-not-contact list by {actor(user)} "
            f"({body.reason}) and closed."
        ),
        campaign_id=prospect.campaign_id,
        campaign_name=campaign.name if campaign else None,
        actor=actor(user),
        payload={"suppression_id": entry.id, "scope": body.scope},
    )
    await session.commit()
    return {"prospect": serialize_prospect(prospect), "suppression_id": entry.id}


__all__ = ["router", "serialize_agent_run", "serialize_signal"]
