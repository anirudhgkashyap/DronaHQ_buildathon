"""Per-campaign agent configuration and the agent-level pause.

Six agents run inside every campaign, and each one is separately configurable
and separately stoppable. That middle level of control matters: when the
personalisation agent starts writing badly, the answer is to pause *it* while
research and qualification keep filling the funnel — not to stop the campaign
and lose a day of pipeline.

Each row also carries what the run actually cost: runs, failures, average
latency and total spend, read in one grouped query rather than one per agent.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit
from ..agents.schemas import AGENT_LABELS
from ..db import new_id
from ..models import AGENT_KEYS, AgentConfig, AgentRun, utcnow
from ..serializers import serialize_agent_config
from .deps import SessionDep, UserDep, actor, load_campaign, require_choice

router = APIRouter(tags=["agents"])

AGENT_STATES = ("pause", "resume")


class AgentConfigUpdate(BaseModel):
    """All optional: a PATCH that only flips ``enabled`` must not reset the
    model or wipe the escalation rules."""

    enabled: Optional[bool] = None
    model: Optional[str] = Field(default=None, max_length=80)
    tools: Optional[list[str]] = None
    thresholds: Optional[dict[str, Any]] = None
    escalation_rules: Optional[dict[str, Any]] = None
    dronahq_agent_id: Optional[str] = Field(default=None, max_length=160)


async def agent_stats(session: AsyncSession, campaign_id: str) -> dict[str, dict]:
    """``{agent_key: stats}`` for a campaign, in a single grouped query.

    ``case`` sums rather than one COUNT per status: six agents times five
    statuses would otherwise be thirty round trips to render one panel.
    """
    rows = await session.execute(
        select(
            AgentRun.agent_key,
            func.count(AgentRun.id),
            func.sum(case((AgentRun.status == "succeeded", 1), else_=0)),
            func.sum(case((AgentRun.status == "failed", 1), else_=0)),
            func.sum(case((AgentRun.status == "skipped", 1), else_=0)),
            func.avg(AgentRun.latency_ms),
            func.coalesce(func.sum(AgentRun.cost_usd), 0.0),
            func.coalesce(func.sum(AgentRun.tokens_in), 0),
            func.coalesce(func.sum(AgentRun.tokens_out), 0),
        )
        .where(AgentRun.campaign_id == campaign_id)
        .group_by(AgentRun.agent_key)
    )

    stats: dict[str, dict] = {}
    for key, runs, ok, failed, skipped, latency, cost, tokens_in, tokens_out in rows:
        runs = int(runs or 0)
        ok = int(ok or 0)
        stats[key] = {
            "runs": runs,
            "succeeded": ok,
            "failed": int(failed or 0),
            "skipped": int(skipped or 0),
            "avg_latency_ms": int(latency or 0),
            "total_cost_usd": round(float(cost or 0.0), 6),
            "tokens_in": int(tokens_in or 0),
            "tokens_out": int(tokens_out or 0),
            # None, not 0.0, with no runs: "never ran" is not "always fails".
            "success_rate": round(ok / runs, 4) if runs else None,
        }
    return stats


@router.get("/campaigns/{campaign_id}/agents")
async def list_agents(campaign_id: str, session: SessionDep, _: UserDep) -> dict:
    """Every agent in the campaign with its configuration and its record.

    A campaign that predates an agent being added still lists it, with an
    implicit default config, so the panel never has a hole in it.
    """
    await load_campaign(session, campaign_id)

    configs = {
        config.agent_key: config
        for config in (
            await session.execute(select(AgentConfig).where(AgentConfig.campaign_id == campaign_id))
        ).scalars()
    }
    stats = await agent_stats(session, campaign_id)

    items = []
    for agent_key in AGENT_KEYS:
        config = configs.get(agent_key)
        if config is None:
            # Not persisted: shown so the UI is complete, and written the first
            # time someone actually changes it.
            config = AgentConfig(
                id=f"pending_{agent_key}", campaign_id=campaign_id, agent_key=agent_key
            )
            config.enabled, config.state = True, "active"
            config.model, config.tools = "claude-sonnet-4-6", []
            config.thresholds, config.escalation_rules = {}, {}
            config.dronahq_agent_id, config.updated_at = None, None
        payload = serialize_agent_config(config, stats.get(agent_key, {}))
        payload["label"] = AGENT_LABELS.get(agent_key, agent_key)
        items.append(payload)

    return {"items": items}


async def _ensure_config(session: AsyncSession, campaign_id: str, agent_key: str) -> AgentConfig:
    """Fetch the config row, creating the default one if it never existed.

    Campaigns created before an agent was added have no row for it. Writing one
    lazily on first edit is preferable to 404-ing on a control the UI is
    showing, and to back-filling every campaign on deploy.
    """
    config = (
        await session.execute(
            select(AgentConfig).where(
                AgentConfig.campaign_id == campaign_id, AgentConfig.agent_key == agent_key
            )
        )
    ).scalar_one_or_none()
    if config is not None:
        return config

    config = AgentConfig(
        id=new_id("acfg"), campaign_id=campaign_id, agent_key=agent_key, enabled=True
    )
    session.add(config)
    await session.flush()
    return config


@router.patch("/campaigns/{campaign_id}/agents/{agent_key}")
async def update_agent(
    campaign_id: str,
    agent_key: str,
    body: AgentConfigUpdate,
    session: SessionDep,
    user: UserDep,
) -> dict:
    """Change one agent's model, tools, thresholds or escalation rules.

    Every field here shapes behaviour that reaches a prospect, so the change is
    audited with the exact set of fields touched.
    """
    campaign = await load_campaign(session, campaign_id)
    require_choice(agent_key, AGENT_KEYS, "agent")
    config = await _ensure_config(session, campaign_id, agent_key)

    changes = body.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(config, field, value)
    config.updated_at = utcnow()
    await session.flush()

    label = AGENT_LABELS.get(agent_key, agent_key)
    if changes:
        await audit.record_campaign_event(
            session,
            campaign,
            event_type="agent_config_updated",
            message=f"{label} reconfigured by {actor(user)}: {', '.join(sorted(changes))}.",
            actor=actor(user),
            payload={"agent_key": agent_key, "fields": sorted(changes)},
        )
    await session.commit()

    stats = await agent_stats(session, campaign_id)
    payload = serialize_agent_config(config, stats.get(agent_key, {}))
    payload["label"] = label
    return payload


@router.post("/campaigns/{campaign_id}/agents/{agent_key}/{state}")
async def set_agent_state(
    campaign_id: str, agent_key: str, state: str, session: SessionDep, user: UserDep
) -> dict:
    """Pause or resume one agent inside one campaign.

    The guardrail layer checks this before every invocation, including pure
    reasoning steps, so a paused agent stops costing money as well as stops
    acting. The rest of the campaign carries on.
    """
    campaign = await load_campaign(session, campaign_id)
    require_choice(agent_key, AGENT_KEYS, "agent")
    require_choice(state, AGENT_STATES, "agent action")

    config = await _ensure_config(session, campaign_id, agent_key)
    config.state = "paused" if state == "pause" else "active"
    config.updated_at = utcnow()
    await session.flush()

    label = AGENT_LABELS.get(agent_key, agent_key)
    await audit.record_campaign_event(
        session,
        campaign,
        event_type="agent_paused" if state == "pause" else "agent_resumed",
        severity="warning" if state == "pause" else "success",
        message=(
            f"{label} paused by {actor(user)}. The rest of the campaign keeps running."
            if state == "pause"
            else f"{label} resumed by {actor(user)}."
        ),
        actor=actor(user),
        payload={"agent_key": agent_key, "state": config.state},
    )
    await session.commit()

    stats = await agent_stats(session, campaign_id)
    payload = serialize_agent_config(config, stats.get(agent_key, {}))
    payload["label"] = label
    return payload


__all__ = ["router", "agent_stats"]
