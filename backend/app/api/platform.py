"""Platform-wide state: identity, activity feed, kill switch, scheduler,
integrations, the do-not-contact list, representatives, and inbound webhooks.

The theme of this router is *operational control*. Everything here answers one
of two questions a manager asks when something looks wrong: "what has the
system been doing?" (events, scheduler) and "how do I make it stop?" (kill
switch, suppression, channel and campaign pauses live next door).
"""
from __future__ import annotations

import time
from typing import Annotated, Any, Literal, Optional

import httpx
from fastapi import APIRouter, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, or_, select

from .. import audit
from ..agents.client import AGENT_ID_SETTINGS, resolve_dronahq_agent_id
from ..agents.schemas import AGENT_LABELS
from ..config import settings
from ..db import new_id
from ..models import AuditLog, Campaign, PlatformSetting, Prospect, SuppressionEntry, User, utcnow
from ..orchestrator import scheduler
from ..orchestrator.engine import handle_inbound
from ..orchestrator.guardrails import get_kill_switch, set_kill_switch
from ..rag.embedder import EMBEDDER_NAME
from ..serializers import iso, serialize_event, serialize_user
from .deps import SessionDep, UserDep, actor, clamp, load_prospect, step_json
from .errors import ApiError, invalid_request, not_found

router = APIRouter(tags=["platform"])

ChannelType = Literal["linkedin", "email", "voice", "sms", "whatsapp"]


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------
class KillSwitchRequest(BaseModel):
    engaged: bool


class SuppressionCreate(BaseModel):
    """At least one identifier is required: an entry that matches nothing is a
    silent no-op, and a do-not-contact list that silently does nothing is the
    most dangerous object in the system."""

    email: Optional[str] = None
    phone: Optional[str] = None
    linkedin_url: Optional[str] = None
    domain: Optional[str] = None
    reason: str = Field(default="manual", max_length=120)
    scope: Literal["global", "campaign"] = "global"
    campaign_id: Optional[str] = None

    @model_validator(mode="after")
    def _needs_an_identifier(self) -> "SuppressionCreate":
        if not any([self.email, self.phone, self.linkedin_url, self.domain]):
            raise ValueError(
                "needs at least one of email, phone, LinkedIn URL or company domain"
            )
        if self.scope == "campaign" and not self.campaign_id:
            raise ValueError("needs a campaign when the scope is limited to one campaign")
        return self


class UserCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    initials: Optional[str] = Field(default=None, max_length=8)
    email: Optional[str] = None
    role: Literal["manager", "rep", "admin"] = "rep"
    daily_activity_limit: int = Field(default=100, ge=0, le=10_000)
    channels_available: list[ChannelType] = Field(default_factory=lambda: ["email", "linkedin"])
    working_hours: Optional[dict[str, Any]] = None


class InboundWebhook(BaseModel):
    channel: ChannelType
    body: str = Field(min_length=1)
    campaign_id: Optional[str] = None
    prospect_id: Optional[str] = None
    email: Optional[str] = None
    external_id: Optional[str] = None

    @model_validator(mode="after")
    def _identifies_someone(self) -> "InboundWebhook":
        if not (self.prospect_id or self.email):
            raise ValueError("must name either a prospect or the email address that replied")
        return self


def serialize_suppression(entry: SuppressionEntry) -> dict:
    return {
        "id": entry.id,
        "email": entry.email,
        "phone": entry.phone,
        "linkedin_url": entry.linkedin_url,
        "domain": entry.domain,
        "reason": entry.reason,
        "scope": entry.scope,
        "campaign_id": entry.campaign_id,
        "created_at": iso(entry.created_at),
    }


# ---------------------------------------------------------------------------
# Identity and activity
# ---------------------------------------------------------------------------
@router.get("/me")
async def me(user: UserDep) -> dict:
    """The signed-in user. Drives the avatar in the top bar."""
    return serialize_user(user)


@router.get("/events")
async def list_events(
    session: SessionDep,
    _: UserDep,
    limit: Annotated[Optional[int], Query(ge=1, le=200)] = 5,
    campaign_id: Annotated[Optional[str], Query()] = None,
) -> dict:
    """The activity feed, newest first.

    Straight off the append-only audit log, which is deliberate: the feed and
    the evidence trail are the same data, so what a manager sees on the page is
    exactly what an auditor would see in the table.
    """
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
    if campaign_id:
        stmt = stmt.where(AuditLog.campaign_id == campaign_id)
    entries = (await session.execute(stmt.limit(clamp(limit, 5, 200)))).scalars().all()
    return {"items": [serialize_event(entry) for entry in entries]}


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------
@router.get("/platform/kill-switch")
async def read_kill_switch(session: SessionDep, _: UserDep) -> dict:
    return await get_kill_switch(session)


@router.put("/platform/kill-switch")
async def write_kill_switch(
    body: KillSwitchRequest, session: SessionDep, user: UserDep
) -> dict:
    """Stop, or restart, every autonomous external action platform-wide.

    Campaign statuses are untouched on purpose: engaging the switch is an
    emergency brake, not a reconfiguration, and releasing it must put the
    platform back exactly as it was without a manager re-activating twenty
    campaigns by hand.
    """
    state = await set_kill_switch(
        session, engaged=body.engaged, actor={"id": user.id, "name": user.name}
    )

    message = (
        f"Global kill switch engaged by {actor(user)}. "
        "All autonomous execution halted platform-wide."
        if body.engaged
        else f"Global kill switch released by {actor(user)}. Campaigns resume on their own status."
    )
    await audit.record(
        session,
        entity_type="platform",
        entity_id="kill_switch",
        event_type="kill_switch_engaged" if body.engaged else "kill_switch_released",
        severity="error" if body.engaged else "success",
        message=message,
        actor=actor(user),
        payload={"engaged": body.engaged},
    )
    await session.commit()
    return state


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
@router.get("/platform/scheduler")
async def scheduler_state(_: UserDep) -> dict:
    """Whether the autonomous loop is actually running, and what it last did."""
    return scheduler.state.as_dict()


@router.post("/platform/scheduler/tick")
async def scheduler_tick(_: UserDep) -> dict:
    """Force one pass over every live campaign.

    Runs in its own session (``scheduler.run_once`` opens one and commits per
    campaign), so a single campaign blowing up cannot roll back the work the
    others already did.
    """
    return await scheduler.run_once()


# ---------------------------------------------------------------------------
# Integrations
# ---------------------------------------------------------------------------
@router.get("/platform/settings")
async def platform_settings(_: UserDep) -> dict:
    """Which integrations are wired up, as booleans only.

    Never returns a key, a token or a fragment of one. "Is SendGrid
    configured?" is the only question the UI needs answered, and it is the only
    question that can be answered without creating a way to exfiltrate secrets
    through the API.
    """
    return {
        "dronahq_configured": settings.dronahq_enabled,
        "openai_configured": bool(settings.OPENAI_API_KEY),
        "sendgrid_configured": bool(settings.SENDGRID_API_KEY),
        "twilio_configured": bool(settings.TWILIO_ACCOUNT_SID and settings.TWILIO_AUTH_TOKEN),
        "channels_dry_run": settings.CHANNELS_DRY_RUN,
        "embedder": EMBEDDER_NAME,
        "agent_executor": "dronahq" if settings.dronahq_enabled else "simulator",
        "database": "postgres" if settings.is_postgres else "sqlite",
        "scheduler_enabled": settings.SCHEDULER_ENABLED,
        "scheduler_tick_seconds": settings.SCHEDULER_TICK_SECONDS,
        "environment": settings.ENV,
    }


# ---------------------------------------------------------------------------
# Suppression list
# ---------------------------------------------------------------------------
@router.get("/platform/suppression")
async def list_suppression(
    session: SessionDep,
    _: UserDep,
    q: Annotated[Optional[str], Query()] = None,
    limit: Annotated[Optional[int], Query(ge=1, le=500)] = 100,
) -> dict:
    stmt = select(SuppressionEntry).order_by(SuppressionEntry.created_at.desc())
    if q:
        needle = f"%{q.strip().lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(SuppressionEntry.email).like(needle),
                func.lower(SuppressionEntry.domain).like(needle),
                func.lower(SuppressionEntry.linkedin_url).like(needle),
                SuppressionEntry.phone.like(needle),
            )
        )
    total = int(
        (await session.execute(select(func.count(SuppressionEntry.id)))).scalar() or 0
    )
    entries = (await session.execute(stmt.limit(clamp(limit, 100, 500)))).scalars().all()
    return {"items": [serialize_suppression(e) for e in entries], "total": total}


@router.post("/platform/suppression", status_code=201)
async def add_suppression(
    body: SuppressionCreate, session: SessionDep, user: UserDep
) -> dict:
    """Add a do-not-contact entry. Takes effect on the very next guardrail
    check, which is before every outbound message on every channel."""
    entry = SuppressionEntry(
        id=new_id("sup"),
        email=(body.email or "").strip().lower() or None,
        phone=(body.phone or "").strip() or None,
        linkedin_url=(body.linkedin_url or "").strip() or None,
        domain=(body.domain or "").strip().lower() or None,
        reason=body.reason,
        scope=body.scope,
        campaign_id=body.campaign_id,
    )
    session.add(entry)
    await session.flush()

    await audit.record(
        session,
        entity_type="suppression",
        entity_id=entry.id,
        event_type="suppression_added",
        severity="warning",
        message=(
            f"{entry.email or entry.phone or entry.linkedin_url or entry.domain} added to the "
            f"do-not-contact list by {actor(user)} ({entry.reason})."
        ),
        campaign_id=body.campaign_id,
        actor=actor(user),
        payload={"scope": entry.scope, "reason": entry.reason},
    )
    await session.commit()
    return serialize_suppression(entry)


@router.delete("/platform/suppression/{entry_id}")
async def remove_suppression(entry_id: str, session: SessionDep, user: UserDep) -> dict:
    """Remove an entry. The audit row stays: a suppression that was lifted is
    exactly the kind of decision someone will need to explain later."""
    entry = await session.get(SuppressionEntry, entry_id)
    if entry is None:
        raise not_found("suppression entry", entry_id)

    identifier = entry.email or entry.phone or entry.linkedin_url or entry.domain
    await audit.record(
        session,
        entity_type="suppression",
        entity_id=entry.id,
        event_type="suppression_removed",
        severity="warning",
        message=f"{identifier} removed from the do-not-contact list by {actor(user)}.",
        campaign_id=entry.campaign_id,
        actor=actor(user),
    )
    await session.delete(entry)
    await session.commit()
    return {"deleted": entry_id}


# ---------------------------------------------------------------------------
# Representatives
# ---------------------------------------------------------------------------
@router.get("/users")
async def list_users(session: SessionDep, _: UserDep) -> dict:
    users = (await session.execute(select(User).order_by(User.created_at.asc()))).scalars().all()
    return {"items": [serialize_user(u) for u in users]}


@router.post("/users", status_code=201)
async def create_user(body: UserCreate, session: SessionDep, actor_user: UserDep) -> dict:
    """Add a representative.

    Their working hours and daily activity limit are enforced by the guardrail
    layer, so this is a safety-relevant record, not just a name on a card.
    """
    initials = body.initials or "".join(part[0] for part in body.name.split()[:2]).upper() or "?"
    user = User(
        id=new_id("usr"),
        name=body.name.strip(),
        initials=initials,
        email=(body.email or "").strip().lower() or None,
        role=body.role,
        daily_activity_limit=body.daily_activity_limit,
        channels_available=list(body.channels_available),
    )
    if body.working_hours:
        user.working_hours = body.working_hours
    session.add(user)
    await session.flush()

    await audit.record(
        session,
        entity_type="user",
        entity_id=user.id,
        event_type="user_created",
        severity="success",
        message=f"{user.name} added as a {user.role} by {actor(actor_user)}.",
        actor=actor(actor_user),
    )
    await session.commit()
    return serialize_user(user)


# ---------------------------------------------------------------------------
# Inbound webhook
# ---------------------------------------------------------------------------
@router.post("/webhooks/inbound")
async def inbound(body: InboundWebhook, session: SessionDep, _: UserDep) -> dict:
    """Receive a reply from any channel and hand it to the conversation agent.

    Resolution is by prospect id when the provider echoes one back, and by
    email address otherwise — that is what a real mailbox webhook gives you.
    The lookup is deliberately not campaign-scoped unless the caller scopes it,
    because a reply arrives at an inbox, not at a campaign.

    Inbound is accepted even while the kill switch is engaged: refusing to
    *listen* loses data, and only outbound action is dangerous. ``handle_inbound``
    stores the reply first and only then asks an agent what to do, so a halted
    platform still has a complete record when it comes back.
    """
    prospect: Optional[Prospect] = None
    if body.prospect_id:
        prospect = await load_prospect(session, body.prospect_id)
    else:
        stmt = (
            select(Prospect)
            .where(Prospect.email == (body.email or "").strip().lower())
            .order_by(Prospect.updated_at.desc())
        )
        if body.campaign_id:
            stmt = stmt.where(Prospect.campaign_id == body.campaign_id)
        prospect = (await session.execute(stmt.limit(1))).scalars().first()

    if prospect is None:
        raise not_found("prospect for that reply", body.email or body.prospect_id)

    if body.campaign_id and prospect.campaign_id != body.campaign_id:
        raise invalid_request(
            "That prospect belongs to a different campaign than the one named in the webhook."
        )

    campaign = await session.get(Campaign, prospect.campaign_id)
    if campaign is None:
        raise not_found("campaign", prospect.campaign_id)

    result = await handle_inbound(
        session,
        campaign,
        prospect,
        channel=body.channel,
        body=body.body,
        external_id=body.external_id,
    )
    await session.commit()
    return step_json(result)


# ---------------------------------------------------------------------------
# Settings (the global settings page)
# ---------------------------------------------------------------------------
# PlatformSetting is a single-row-per-key JSON store; these are the keys the
# settings page reads and writes. Everything else on /platform/settings
# (above) is derived/read-only integration status, not user-editable config.
_SETTINGS_DEFAULTS: dict[str, Any] = {
    "company_name": {"value": "Atlas SDR"},
    "timezone": {"value": "Asia/Kolkata"},
    "working_hours": {"start": "09:00", "end": "18:00"},
    "working_days": {"days": ["mon", "tue", "wed", "thu", "fri"]},
    "global_daily_send_cap": {"value": 500},
    "dry_run": {"value": settings.CHANNELS_DRY_RUN},
    "channels": {
        "email": {"from_email": settings.OUTREACH_FROM_EMAIL, "from_name": "Atlas SDR", "reply_to": ""},
        "sms": {"number": ""},
        "linkedin": {"daily_limit": 20},
    },
}


async def _get_setting(session, key: str) -> Any:
    row = await session.get(PlatformSetting, key)
    return row.value if row else _SETTINGS_DEFAULTS.get(key)


async def _put_setting(session, key: str, value: Any) -> None:
    row = await session.get(PlatformSetting, key)
    if row is None:
        session.add(PlatformSetting(key=key, value=value))
    else:
        row.value = value
        row.updated_at = utcnow()


@router.get("/settings")
async def read_settings(session: SessionDep, _: UserDep) -> dict:
    """Everything the global settings page's General/Channels/Agents tabs
    render, in one call. Agent connection status is derived from whichever
    DronaHQ webhook URL each agent key resolves to (config/.env), not stored."""
    company = await _get_setting(session, "company_name")
    timezone_ = await _get_setting(session, "timezone")
    working_hours = await _get_setting(session, "working_hours")
    working_days = await _get_setting(session, "working_days")
    daily_cap = await _get_setting(session, "global_daily_send_cap")
    dry_run = await _get_setting(session, "dry_run")
    channels = await _get_setting(session, "channels")

    agents: dict[str, dict] = {}
    for agent_key in AGENT_ID_SETTINGS:
        url = resolve_dronahq_agent_id(agent_key)
        agents[agent_key] = {
            "webhook_url": url,
            "status": "connected" if url else "disconnected",
            "last_ping": None,
        }

    return {
        "company_name": (company or {}).get("value", "Atlas SDR"),
        "timezone": (timezone_ or {}).get("value", "Asia/Kolkata"),
        "working_hours": working_hours or {"start": "09:00", "end": "18:00"},
        "working_days": (working_days or {}).get("days", ["mon", "tue", "wed", "thu", "fri"]),
        "global_daily_limit": (daily_cap or {}).get("value", 500),
        "dry_run": bool((dry_run or {}).get("value", settings.CHANNELS_DRY_RUN)),
        "channels": channels or _SETTINGS_DEFAULTS["channels"],
        "agents": agents,
    }


class GeneralSettingsUpdate(BaseModel):
    company_name: Optional[str] = Field(default=None, max_length=200)
    timezone: Optional[str] = Field(default=None, max_length=64)
    working_hours: Optional[dict[str, Any]] = None
    working_days: Optional[list[str]] = None
    global_daily_limit: Optional[int] = Field(default=None, ge=1, le=100_000)
    dry_run: Optional[bool] = None


@router.put("/settings")
async def write_settings(
    body: GeneralSettingsUpdate, session: SessionDep, user: UserDep
) -> dict:
    """Save the General tab. Every field is optional so the page can PUT its
    whole form without the caller having to know which fields changed."""
    changes = body.model_dump(exclude_unset=True)
    if "company_name" in changes:
        await _put_setting(session, "company_name", {"value": changes["company_name"]})
    if "timezone" in changes:
        await _put_setting(session, "timezone", {"value": changes["timezone"]})
    if "working_hours" in changes:
        await _put_setting(session, "working_hours", changes["working_hours"])
    if "working_days" in changes:
        await _put_setting(session, "working_days", {"days": changes["working_days"]})
    if "global_daily_limit" in changes:
        await _put_setting(session, "global_daily_send_cap", {"value": changes["global_daily_limit"]})
    if "dry_run" in changes:
        await _put_setting(session, "dry_run", {"value": changes["dry_run"]})

    await audit.record(
        session,
        entity_type="platform",
        entity_id="settings",
        event_type="settings_updated",
        severity="info",
        message=f"platform settings updated by {actor(user)}: {', '.join(sorted(changes)) or 'no fields'}.",
        actor=actor(user),
        payload={"fields": sorted(changes)},
    )
    await session.commit()
    return {"ok": True}


class ChannelSettingsUpdate(BaseModel):
    email: Optional[dict[str, Any]] = None
    sms: Optional[dict[str, Any]] = None
    linkedin: Optional[dict[str, Any]] = None


@router.put("/settings/channels")
async def write_channel_settings(
    body: ChannelSettingsUpdate, session: SessionDep, user: UserDep
) -> dict:
    """Save the Channels tab. Merges into the stored ``channels`` blob rather
    than replacing it, so saving the email form doesn't blank out the SMS
    number nobody touched this time.

    Provider API keys (SendGrid, Twilio) are accepted here but never echoed
    back by ``GET /settings`` — same secret-hygiene rule as
    ``/platform/settings``. They're recorded as "configured" only; wiring a
    key through to actually change ``SENDGRID_API_KEY``/``TWILIO_*`` at
    runtime needs a process restart (they're read once at startup), so this
    endpoint stores the non-secret parts and flags the secret ones for the
    deployment's environment variables instead of pretending to hot-swap them.
    """
    current = (await _get_setting(session, "channels")) or _SETTINGS_DEFAULTS["channels"]
    merged = dict(current)
    changes = body.model_dump(exclude_unset=True)
    for channel, fields in changes.items():
        if fields is None:
            continue
        existing = dict(merged.get(channel) or {})
        # Strip secret fields before persisting; they belong in env vars.
        safe_fields = {k: v for k, v in fields.items() if k not in {"api_key", "auth_token"}}
        existing.update(safe_fields)
        merged[channel] = existing

    await _put_setting(session, "channels", merged)

    await audit.record(
        session,
        entity_type="platform",
        entity_id="settings.channels",
        event_type="channel_settings_updated",
        severity="info",
        message=f"channel settings updated by {actor(user)}: {', '.join(sorted(changes)) or 'no fields'}.",
        actor=actor(user),
        payload={"fields": sorted(changes)},
    )
    await session.commit()
    return {"ok": True}


@router.post("/settings/agents/{agent_key}/test")
async def test_agent_webhook(agent_key: str, _: UserDep) -> dict:
    """Ping this agent's configured DronaHQ webhook and report round-trip
    latency, so a manager can tell "wired up" from "wired up and reachable"
    before trusting it in a live campaign."""
    if agent_key not in AGENT_ID_SETTINGS:
        raise invalid_request(
            f"{agent_key!r} is not a known agent. Choose one of: {', '.join(AGENT_ID_SETTINGS)}"
        )
    url = resolve_dronahq_agent_id(agent_key)
    if not url:
        raise ApiError(
            422,
            "not_configured",
            f"No webhook URL is configured for {AGENT_LABELS.get(agent_key, agent_key)}. "
            f"Set {AGENT_ID_SETTINGS[agent_key]} in the environment.",
        )

    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                url,
                json={"ping": True, "agent_key": agent_key},
                headers={"Authorization": f"Bearer {settings.DRONAHQ_API_KEY}"} if settings.DRONAHQ_API_KEY else {},
            )
        latency_ms = int((time.monotonic() - started) * 1000)
        if response.status_code >= 400:
            raise ApiError(
                502,
                "webhook_error",
                f"Webhook responded with HTTP {response.status_code} after {latency_ms}ms.",
            )
        return {"status": "ok", "latency_ms": latency_ms}
    except ApiError:
        raise
    except httpx.HTTPError as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        raise ApiError(
            502,
            "webhook_unreachable",
            f"Could not reach the webhook after {latency_ms}ms: {exc}",
        )


__all__ = ["router"]
