"""The conversation inbox, and human takeover.

A thread is one prospect on one channel. The inbox lists them; opening one
shows the whole exchange; replying takes the conversation away from the agent
and hands it to a person.

Human takeover still goes through the channel adapters and still respects the
platform's hard stops. A manager typing a reply is a *better-informed* sender,
not an exempt one: the kill switch and the do-not-contact list exist for
reasons that do not stop applying because a human is at the keyboard.
"""
from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from .. import audit
from ..channels.registry import get_adapter
from ..db import new_id
from ..models import (
    Approval,
    Campaign,
    Company,
    ConversationThread,
    Message,
    Prospect,
    utcnow,
)
from ..orchestrator.guardrails import check_kill_switch, check_suppression
from ..serializers import serialize_message, serialize_prospect, serialize_thread
from .deps import SessionDep, UserDep, actor, clamp, load_thread
from .errors import blocked, not_found

router = APIRouter(tags=["conversations"])


class ReplyRequest(BaseModel):
    body: str = Field(min_length=1, max_length=20_000)
    subject: Optional[str] = Field(default=None, max_length=400)


class NoteRequest(BaseModel):
    note: Optional[str] = Field(default=None, max_length=2000)


async def _last_messages(session, thread_ids: list[str]) -> dict[str, Message]:
    """The most recent message for each thread, in ONE query.

    The obvious implementation — a query per thread for its preview — is the
    classic N+1 that makes an inbox feel slow exactly when it is busiest. Here
    the whole set is read in creation order and the dict keeps overwriting, so
    the last write per thread is the newest message.
    """
    if not thread_ids:
        return {}
    rows = (
        await session.execute(
            select(Message)
            .where(Message.thread_id.in_(thread_ids))
            .order_by(Message.created_at.asc())
        )
    ).scalars().all()
    latest: dict[str, Message] = {}
    for message in rows:
        latest[message.thread_id] = message
    return latest


@router.get("/conversations")
async def list_conversations(
    session: SessionDep,
    _: UserDep,
    campaign_id: Annotated[Optional[str], Query()] = None,
    status: Annotated[Optional[str], Query()] = None,
    channel: Annotated[Optional[str], Query()] = None,
    q: Annotated[Optional[str], Query()] = None,
    limit: Annotated[Optional[int], Query(ge=1, le=200)] = 50,
) -> dict:
    """The inbox, most recently active first."""
    clauses = []
    if campaign_id:
        clauses.append(ConversationThread.campaign_id == campaign_id)
    if status and status != "all":
        clauses.append(ConversationThread.status == status)
    if channel:
        clauses.append(ConversationThread.channel == channel)

    stmt = select(ConversationThread)
    if q:
        needle = f"%{q.strip().lower()}%"
        stmt = stmt.outerjoin(Prospect, Prospect.id == ConversationThread.prospect_id).outerjoin(
            Company, Company.id == Prospect.company_id
        )
        clauses.append(
            or_(
                func.lower(Prospect.full_name).like(needle),
                func.lower(Prospect.email).like(needle),
                func.lower(Company.name).like(needle),
            )
        )

    count_stmt = select(func.count(ConversationThread.id)).select_from(ConversationThread)
    if q:
        count_stmt = count_stmt.outerjoin(
            Prospect, Prospect.id == ConversationThread.prospect_id
        ).outerjoin(Company, Company.id == Prospect.company_id)
    total = int((await session.execute(count_stmt.where(*clauses))).scalar() or 0)

    threads = (
        await session.execute(
            stmt.where(*clauses)
            .order_by(ConversationThread.last_activity_at.desc())
            .limit(clamp(limit, 50, 200))
        )
    ).scalars().all()

    previews = await _last_messages(session, [t.id for t in threads])
    return {
        "items": [serialize_thread(t, previews.get(t.id)) for t in threads],
        "total": total,
    }


@router.get("/conversations/{thread_id}")
async def get_conversation(thread_id: str, session: SessionDep, _: UserDep) -> dict:
    """One thread, in full, oldest message first."""
    thread = await load_thread(session, thread_id)
    messages = (
        await session.execute(
            select(Message)
            .where(Message.thread_id == thread_id)
            .order_by(Message.created_at.asc())
        )
    ).scalars().all()

    payload = serialize_thread(thread, messages[-1] if messages else None)
    payload["messages"] = [serialize_message(m) for m in messages]
    payload["prospect"] = serialize_prospect(thread.prospect) if thread.prospect else None
    return payload


@router.post("/conversations/{thread_id}/reply")
async def reply(
    thread_id: str, body: ReplyRequest, session: SessionDep, user: UserDep
) -> dict:
    """Human takeover: send this text, now, as the campaign's sender.

    Two guardrails still apply and both return 409 rather than sending:

    * the **global kill switch** — when the platform is halted it is halted,
      and a manual send is exactly the kind of "just this once" that makes an
      emergency brake useless;
    * the **do-not-contact list** — an opt-out is a commitment, not a default.

    Everything else (campaign status, channel pauses, rate limits, working
    hours) is deliberately *not* enforced here. Those exist to govern what the
    machine does unsupervised; a human deciding to answer a live conversation
    is the supervision they were protecting.
    """
    thread = await load_thread(session, thread_id)
    prospect = thread.prospect
    if prospect is None:
        raise not_found("prospect for this conversation", thread.prospect_id)
    campaign = await session.get(Campaign, thread.campaign_id)
    if campaign is None:
        raise not_found("campaign", thread.campaign_id)

    kill = await check_kill_switch(session)
    if not kill.allowed:
        raise blocked(kill.code, kill.reason)

    suppressed = await check_suppression(session, prospect)
    if not suppressed.allowed:
        raise blocked(suppressed.code, suppressed.reason)

    message_id = new_id("msg")
    message = Message(
        id=message_id,
        thread_id=thread.id,
        campaign_id=campaign.id,
        prospect_id=prospect.id,
        channel=thread.channel,
        direction="outbound",
        message_type="reply",
        subject=body.subject,
        body=body.body,
        status="draft",
        # Human replies are one-offs, so the key only has to be unique; there
        # is no earlier attempt for a retry to converge on.
        idempotency_key=f"human_reply:{message_id}",
    )
    session.add(message)
    await session.flush()

    # The adapter owns CHANNELS_DRY_RUN, so a demo deployment records the reply
    # in full without contacting a real person.
    adapter = get_adapter(thread.channel)
    result = await adapter.send(
        recipient={
            "email": prospect.email,
            "phone": prospect.phone,
            "linkedin_url": prospect.linkedin_url,
            "name": prospect.full_name,
        },
        subject=message.subject,
        body=message.body,
        campaign=campaign,
        prospect=prospect,
    )

    message.status = result.status
    message.external_id = result.external_id
    message.error = result.error
    if result.ok:
        message.sent_at = utcnow()
        prospect.last_contacted_at = message.sent_at
        prospect.current_channel = thread.channel
        thread.status = "active"
        thread.last_activity_at = utcnow()
    await session.flush()

    await audit.record(
        session,
        entity_type="message",
        entity_id=message.id,
        event_type="human_reply_sent" if result.ok else "human_reply_failed",
        severity="success" if result.ok else "error",
        message=(
            f"{actor(user)} replied to {prospect.full_name} on {thread.channel}."
            if result.ok
            else f"{actor(user)}'s reply to {prospect.full_name} failed: {result.error}"
        ),
        campaign_id=campaign.id,
        campaign_name=campaign.name,
        actor=actor(user),
        payload={"channel": thread.channel, "dry_run": result.dry_run, "prospect_id": prospect.id},
    )
    await session.commit()

    payload = serialize_thread(thread, message)
    payload["message"] = serialize_message(message)
    return payload


@router.post("/conversations/{thread_id}/close")
async def close(
    thread_id: str, body: NoteRequest, session: SessionDep, user: UserDep
) -> dict:
    """Close a conversation and stop the follow-up cadence for that prospect.

    Closing the thread alone would leave the orchestrator queuing the next
    touch, so the prospect is closed with it.
    """
    thread = await load_thread(session, thread_id)
    campaign = await session.get(Campaign, thread.campaign_id)

    thread.status = "closed"
    thread.last_activity_at = utcnow()
    if thread.prospect is not None:
        thread.prospect.status = "closed"
        thread.prospect.next_action_at = None
    await session.flush()

    await audit.record(
        session,
        entity_type="conversation",
        entity_id=thread.id,
        event_type="conversation_closed",
        message=(
            f"conversation with {thread.prospect.full_name if thread.prospect else 'a prospect'} "
            f"closed by {actor(user)}."
            + (f" Note: {body.note}" if body.note else "")
        ),
        campaign_id=thread.campaign_id,
        campaign_name=campaign.name if campaign else None,
        actor=actor(user),
        payload={"note": body.note},
    )
    await session.commit()
    return serialize_thread(thread)


@router.post("/conversations/{thread_id}/escalate")
async def escalate(
    thread_id: str, body: NoteRequest, session: SessionDep, user: UserDep
) -> dict:
    """Flag a conversation for a person to handle.

    Raises a real approval rather than just tinting the row, so the escalation
    lands in the same queue as every agent-raised one. One queue is the only
    way a manager can trust that nothing is waiting somewhere they are not
    looking.
    """
    thread = await load_thread(session, thread_id)
    campaign = await session.get(Campaign, thread.campaign_id)
    prospect = thread.prospect

    reason = body.note or "Escalated by a human reviewer."
    thread.status = "escalated"
    thread.escalation_reason = reason
    thread.last_activity_at = utcnow()
    if prospect is not None:
        prospect.status = "escalated"

    existing = (
        await session.execute(
            select(Approval).where(
                Approval.prospect_id == thread.prospect_id,
                Approval.reason == "Needs human reply",
                Approval.status == "pending",
            )
        )
    ).scalars().first()
    if existing is None:
        session.add(
            Approval(
                id=new_id("apr"),
                campaign_id=thread.campaign_id,
                prospect_id=thread.prospect_id,
                title=(
                    f"{actor(user)} escalated the conversation with "
                    f"{prospect.full_name if prospect else 'a prospect'}"
                ),
                reason="Needs human reply",
                detail=reason,
            )
        )
    await session.flush()

    await audit.record(
        session,
        entity_type="conversation",
        entity_id=thread.id,
        event_type="conversation_escalated",
        severity="warning",
        message=(
            f"conversation with {prospect.full_name if prospect else 'a prospect'} "
            f"escalated by {actor(user)}: {reason}"
        ),
        campaign_id=thread.campaign_id,
        campaign_name=campaign.name if campaign else None,
        actor=actor(user),
        payload={"reason": reason},
    )
    await session.commit()
    return serialize_thread(thread)


__all__ = ["router"]
