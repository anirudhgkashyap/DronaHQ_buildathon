"""Shared request dependencies: identity, entity lookups, common parsing.

Anything more than one router needs lives here, so a fix to "how do we find a
campaign" or "who is acting" lands in one place rather than nine.
"""
from __future__ import annotations

import dataclasses
from typing import Annotated, Any, Iterable, Optional

from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import (
    AgentConfig,
    Approval,
    Campaign,
    ConversationThread,
    KnowledgeDocument,
    Message,
    Prospect,
    User,
)
from .errors import ApiError, invalid_request, not_found

SessionDep = Annotated[AsyncSession, Depends(get_session)]

# The single-tenant demo identity. Real deployments never reach this branch
# because the token resolves first.
DEMO_MANAGER = {
    "id": "usr_sm",
    "name": "S. Menon",
    "initials": "SM",
    "role": "manager",
    "email": "s.menon@atlas.example",
}


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
async def _seeded_manager(session: AsyncSession) -> User:
    """The account every unauthenticated request acts as.

    Prefers the account owner (an ``admin`` row, created first by the seed
    script), falls back to a manager, then to any user, and only creates
    ``usr_sm`` when the table is genuinely empty. That last branch exists so a
    fresh database still serves ``GET /me`` instead of 500-ing on the very
    first page load of the demo.
    """
    admin = (
        await session.execute(select(User).where(User.role == "admin").order_by(User.created_at))
    ).scalars().first()
    if admin:
        return admin

    manager = (
        await session.execute(select(User).where(User.role == "manager").order_by(User.created_at))
    ).scalars().first()
    if manager:
        return manager

    anyone = (await session.execute(select(User).order_by(User.created_at))).scalars().first()
    if anyone:
        return anyone

    manager = User(**DEMO_MANAGER)
    session.add(manager)
    await session.commit()
    return manager


async def current_user(
    session: SessionDep,
    authorization: Annotated[Optional[str], Header()] = None,
) -> User:
    """Resolve the acting user from ``Authorization: Bearer <token>``.

    **This is a demo-grade shim and is deliberately labelled as one.** The
    token *is* the user id: there is no signature, no expiry and no password,
    so anyone who can reach the API can act as anyone. That is acceptable for a
    single-tenant hackathon deployment behind a private URL and nowhere else.

    What replaces it in production: the same dependency signature, with the
    body swapped for "verify a signed JWT (or an httpOnly session cookie — the
    frontend already supports ``SEND_COOKIES``), load the user it names, and
    401 when the signature, audience or expiry does not check out". Every
    router already depends on this function rather than on a header, so the
    swap touches this file only.

    A missing header is *not* an error: the frontend ships with no login page
    yet, so an absent token falls back to the seeded manager rather than
    locking the demo out of its own control plane.
    """
    raw = (authorization or "").strip()
    if not raw:
        return await _seeded_manager(session)

    scheme, _, token = raw.partition(" ")
    token = token.strip() if scheme.lower() == "bearer" else raw
    if not token:
        return await _seeded_manager(session)

    user = await session.get(User, token)
    if user is None:
        # A token that names nobody is a stale session, not a server fault.
        # 401 is what makes js/config.js run onUnauthorized().
        raise ApiError(401, "unauthenticated", "Your session has expired. Sign in again to continue.")
    return user


UserDep = Annotated[User, Depends(current_user)]


def actor(user: User) -> str:
    """The name written into audit rows, which managers read verbatim."""
    return user.name or user.id


# ---------------------------------------------------------------------------
# Entity lookups
# ---------------------------------------------------------------------------
async def load_campaign(session: AsyncSession, campaign_id: str) -> Campaign:
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None:
        raise not_found("campaign", campaign_id)
    return campaign


async def load_prospect(session: AsyncSession, prospect_id: str) -> Prospect:
    prospect = await session.get(Prospect, prospect_id)
    if prospect is None:
        raise not_found("prospect", prospect_id)
    return prospect


async def load_approval(session: AsyncSession, approval_id: str) -> Approval:
    approval = await session.get(Approval, approval_id)
    if approval is None:
        raise not_found("approval", approval_id)
    return approval


async def load_thread(session: AsyncSession, thread_id: str) -> ConversationThread:
    thread = await session.get(ConversationThread, thread_id)
    if thread is None:
        raise not_found("conversation", thread_id)
    return thread


async def load_message(session: AsyncSession, message_id: str) -> Message:
    message = await session.get(Message, message_id)
    if message is None:
        raise not_found("message", message_id)
    return message


async def load_document(session: AsyncSession, document_id: str) -> KnowledgeDocument:
    document = await session.get(KnowledgeDocument, document_id)
    if document is None:
        raise not_found("document", document_id)
    return document


async def load_agent_config(session: AsyncSession, campaign_id: str, agent_key: str) -> AgentConfig:
    config = (
        await session.execute(
            select(AgentConfig).where(
                AgentConfig.campaign_id == campaign_id, AgentConfig.agent_key == agent_key
            )
        )
    ).scalar_one_or_none()
    if config is None:
        raise not_found(f"{agent_key.replace('_', ' ')} agent configuration", agent_key)
    return config


# ---------------------------------------------------------------------------
# Small shared conversions
# ---------------------------------------------------------------------------
def step_json(result: Any) -> dict:
    """``StepResult`` -> JSON. The orchestrator returns dataclasses; the API
    must not leak that choice, and the UI wants the blocked code."""
    if dataclasses.is_dataclass(result):
        return dataclasses.asdict(result)
    return dict(result or {})


def steps_json(results: Iterable[Any]) -> list[dict]:
    return [step_json(r) for r in results or []]


def require_choice(value: str, allowed: Iterable[str], what: str) -> str:
    """Validate a path segment against a closed set.

    Path parameters cannot be validated by pydantic without turning a typo into
    FastAPI's own 422 shape, so enumerations that arrive in the URL are checked
    here and fail with a sentence naming the valid options.
    """
    options = list(allowed)
    if value not in options:
        raise invalid_request(f"{value!r} is not a valid {what}. Choose one of: {', '.join(options)}")
    return value


def clamp(value: Optional[int], default: int, maximum: int) -> int:
    """Bound a client-supplied page size so one request cannot pull the table."""
    if value is None:
        return default
    return max(1, min(int(value), maximum))


__all__ = [
    "SessionDep",
    "UserDep",
    "actor",
    "clamp",
    "current_user",
    "load_agent_config",
    "load_approval",
    "load_campaign",
    "load_document",
    "load_message",
    "load_prospect",
    "load_thread",
    "require_choice",
    "step_json",
    "steps_json",
]
