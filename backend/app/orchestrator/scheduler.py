"""Background scheduler: the loop that makes the system autonomous.

A single asyncio task ticks every live campaign in turn. It is deliberately
simple rather than a distributed queue, because for this system the hard part
is the decisions inside a tick, not the fan-out across machines, and a loop
that a reviewer can read end to end is worth more than one they must trust.

Everything it touches is idempotent, so a missed, duplicated or interrupted
tick is safe: the next tick re-derives the same keys and skips completed work.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import select

from ..config import settings
from ..db import session_scope
from ..models import Campaign, utcnow
from .engine import StepResult, tick_campaign
from .guardrails import get_kill_switch

logger = logging.getLogger(__name__)


@dataclass
class SchedulerState:
    """Observable state, surfaced at /api/v1/platform/scheduler so the UI can
    show whether the autonomous layer is actually running."""

    running: bool = False
    last_tick_at: Optional[dt.datetime] = None
    last_tick_duration_ms: int = 0
    ticks: int = 0
    campaigns_last_tick: int = 0
    actions_last_tick: int = 0
    last_error: Optional[str] = None
    recent: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "running": self.running,
            "enabled": settings.SCHEDULER_ENABLED,
            "tick_seconds": settings.SCHEDULER_TICK_SECONDS,
            "last_tick_at": self.last_tick_at.isoformat().replace("+00:00", "Z")
            if self.last_tick_at
            else None,
            "last_tick_duration_ms": self.last_tick_duration_ms,
            "ticks": self.ticks,
            "campaigns_last_tick": self.campaigns_last_tick,
            "actions_last_tick": self.actions_last_tick,
            "last_error": self.last_error,
            "recent": self.recent[-25:],
        }


state = SchedulerState()
_task: Optional[asyncio.Task] = None


def _summarise(campaign_name: str, results: list[StepResult]) -> list[dict]:
    return [
        {
            "campaign": campaign_name,
            "prospect_id": r.prospect_id,
            "action": r.action,
            "detail": r.detail,
            "blocked_code": r.blocked_code,
            "at": utcnow().isoformat().replace("+00:00", "Z"),
        }
        for r in results
        if r.action not in {"noop"}
    ]


async def run_once(limit_per_campaign: Optional[int] = None) -> dict:
    """Tick every live campaign once. Safe to call by hand from the API."""
    started = utcnow()

    async with session_scope() as session:
        kill = await get_kill_switch(session)
        if kill.get("engaged"):
            state.last_tick_at = started
            state.campaigns_last_tick = 0
            state.actions_last_tick = 0
            return {"halted": True, "reason": "kill switch engaged", "campaigns": 0, "actions": 0}

        campaigns = (
            await session.execute(select(Campaign).where(Campaign.status == "live"))
        ).scalars().all()

        total_actions = 0
        events: list[dict] = []
        for campaign in campaigns:
            try:
                results = await tick_campaign(session, campaign, limit=limit_per_campaign)
                # Commit per campaign so one campaign's failure cannot roll back
                # the work another campaign already did.
                await session.commit()
                events.extend(_summarise(campaign.name, results))
                total_actions += len([r for r in results if r.action not in {"noop", "skipped"}])
            except Exception as exc:
                await session.rollback()
                logger.exception("campaign tick failed: %s", campaign.id)
                state.last_error = f"{campaign.name}: {exc}"

    duration = int((utcnow() - started).total_seconds() * 1000)
    state.last_tick_at = started
    state.last_tick_duration_ms = duration
    state.ticks += 1
    state.campaigns_last_tick = len(campaigns)
    state.actions_last_tick = total_actions
    state.recent.extend(events)
    state.recent = state.recent[-100:]

    return {
        "halted": False,
        "campaigns": len(campaigns),
        "actions": total_actions,
        "duration_ms": duration,
        "events": events,
    }


async def _loop() -> None:
    state.running = True
    logger.info("scheduler started (every %ss)", settings.SCHEDULER_TICK_SECONDS)
    try:
        while True:
            try:
                await run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # the loop must outlive any single failure
                state.last_error = str(exc)
                logger.exception("scheduler tick failed")
            await asyncio.sleep(settings.SCHEDULER_TICK_SECONDS)
    finally:
        state.running = False
        logger.info("scheduler stopped")


def start() -> None:
    global _task
    if not settings.SCHEDULER_ENABLED:
        logger.info("scheduler disabled by configuration")
        return
    if _task and not _task.done():
        return
    _task = asyncio.create_task(_loop(), name="atlas-scheduler")


async def stop() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _task
    _task = None
