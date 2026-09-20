"""Append-only audit trail.

Every meaningful state change goes through ``record``. Nothing in the codebase
updates or deletes an audit row: the table is the evidence a manager uses to
answer "why did the agent do that?", and it records the prompt version that
was active at the moment of the action.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from .db import new_id
from .models import AuditLog


async def record(
    session: AsyncSession,
    *,
    entity_type: str,
    entity_id: str,
    event_type: str,
    message: str,
    campaign_id: Optional[str] = None,
    campaign_name: Optional[str] = None,
    severity: str = "info",
    actor: str = "system",
    payload: Optional[dict] = None,
    prompt_version_id: Optional[str] = None,
) -> AuditLog:
    data = dict(payload or {})
    if campaign_name:
        data["campaign_name"] = campaign_name

    entry = AuditLog(
        campaign_id=campaign_id,
        entity_type=entity_type,
        entity_id=entity_id,
        event_type=event_type,
        severity=severity,
        message=message,
        actor=actor,
        payload=data,
        prompt_version_id=prompt_version_id,
    )
    session.add(entry)
    await session.flush()
    return entry


async def record_campaign_event(
    session: AsyncSession,
    campaign,
    *,
    event_type: str,
    message: str,
    severity: str = "info",
    actor: str = "system",
    payload: Optional[dict] = None,
) -> AuditLog:
    """Convenience wrapper for campaign-scoped events, which are what the
    activity feed on the campaigns page renders."""
    return await record(
        session,
        entity_type="campaign",
        entity_id=campaign.id,
        event_type=event_type,
        message=message,
        campaign_id=campaign.id,
        campaign_name=campaign.name,
        severity=severity,
        actor=actor,
        payload=payload,
    )


__all__ = ["record", "record_campaign_event", "new_id"]
