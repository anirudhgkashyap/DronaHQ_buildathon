"""The human-in-the-loop queue.

Four gates raise approvals — borderline ICP fit, a seniority threshold, a
low-confidence draft, and a cross-campaign conflict — plus anything the
conversation agent decides it should not answer alone. Until a pending
approval is decided, the orchestrator will not advance that prospect, so this
router is the only thing that unblocks them.

Approving is not a rubber stamp: when the approval gates a drafted message,
approving *sends it*, immediately, through the same guardrail layer as an
autonomous send. That is deliberate. A manager who clicks approve expects the
email to go out, not to join another queue.
"""
from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from .. import audit
from ..models import Approval, Campaign, Message, Prospect, utcnow
from ..orchestrator.engine import deliver_message
from ..serializers import serialize_approval, serialize_message, serialize_prospect
from .deps import SessionDep, UserDep, actor, clamp, load_approval, step_json
from .errors import invalid_transition, not_found

router = APIRouter(tags=["approvals"])

#: Rejecting one of these means "do not contact this person at all", so the
#: prospect is closed rather than returned to the queue. Every other rejection
#: is a comment on the *draft*, and the prospect goes back to the orchestrator.
TERMINAL_REJECTION_REASONS = {"Above seniority threshold", "Conflict"}


class DecisionRequest(BaseModel):
    note: Optional[str] = Field(default=None, max_length=2000)


@router.get("/approvals")
async def list_approvals(
    session: SessionDep,
    _: UserDep,
    status: Annotated[str, Query()] = "pending",
    limit: Annotated[Optional[int], Query(ge=1, le=200)] = 20,
    campaign_id: Annotated[Optional[str], Query()] = None,
) -> dict:
    """The queue, newest first.

    ``total`` is the full count matching the filter, not ``len(items)``: the
    campaigns page asks for four and still has to render "7 to review", and a
    badge that undercounts the backlog is worse than no badge.

    Campaign names come from one join, not one lookup per row.
    """
    clauses = []
    if status and status != "all":
        clauses.append(Approval.status == status)
    if campaign_id:
        clauses.append(Approval.campaign_id == campaign_id)

    total = int(
        (
            await session.execute(select(func.count(Approval.id)).where(*clauses))
        ).scalar()
        or 0
    )

    rows = (
        await session.execute(
            select(Approval, Campaign.name)
            .outerjoin(Campaign, Campaign.id == Approval.campaign_id)
            .where(*clauses)
            .order_by(Approval.created_at.desc())
            .limit(clamp(limit, 20, 200))
        )
    ).all()

    return {
        "items": [serialize_approval(approval, campaign_name) for approval, campaign_name in rows],
        "total": total,
    }


@router.get("/approvals/{approval_id}")
async def get_approval(approval_id: str, session: SessionDep, _: UserDep) -> dict:
    """One approval with everything the reviewer needs to decide.

    The draft message and the prospect are included rather than linked: a
    reviewer who has to open two more screens to judge one email will stop
    reviewing carefully, and a rubber-stamped queue is the same as no queue.
    """
    approval = await load_approval(session, approval_id)
    campaign = await session.get(Campaign, approval.campaign_id)

    payload = serialize_approval(approval, campaign.name if campaign else None)

    message = await session.get(Message, approval.message_id) if approval.message_id else None
    payload["message"] = serialize_message(message) if message else None

    prospect = await session.get(Prospect, approval.prospect_id) if approval.prospect_id else None
    payload["prospect"] = serialize_prospect(prospect) if prospect else None

    return payload


def _assert_pending(approval: Approval) -> None:
    if approval.status != "pending":
        raise invalid_transition(
            f"This request was already {approval.status}"
            + (" by a colleague." if approval.decided_by_id else ".")
            + " Refresh the queue to see the current state."
        )


@router.post("/approvals/{approval_id}/approve")
async def approve(
    approval_id: str, body: DecisionRequest, session: SessionDep, user: UserDep
) -> dict:
    """Approve, and act on it.

    Two shapes of approval exist and they resolve differently:

    * **Gating a message** — the draft is marked approved and delivery is
      attempted straight away through ``deliver_message``, which re-runs the
      full guardrail stack. An approval is not an override: if the kill switch
      went on while the message sat in the queue, it still does not send.
    * **Gating a prospect** — there is no draft yet (a borderline ICP score, a
      seniority gate). The prospect is returned to the orchestrator with
      ``next_action_at`` set to now, so the next tick picks it straight up.
    """
    approval = await load_approval(session, approval_id)
    _assert_pending(approval)

    campaign = await session.get(Campaign, approval.campaign_id)
    if campaign is None:
        raise not_found("campaign", approval.campaign_id)

    approval.status = "approved"
    approval.decided_by_id = user.id
    approval.decided_at = utcnow()
    approval.decision_note = body.note

    prospect = await session.get(Prospect, approval.prospect_id) if approval.prospect_id else None
    message = await session.get(Message, approval.message_id) if approval.message_id else None

    delivery: Optional[dict] = None
    if message is not None and prospect is not None:
        message.status = "approved"
        await session.flush()
        delivery = step_json(await deliver_message(session, campaign, prospect, message))
    elif prospect is not None:
        # No draft to send: hand the prospect back to the pipeline.
        prospect.status = "pending"
        prospect.next_action_at = utcnow()
        await session.flush()

    await audit.record(
        session,
        entity_type="approval",
        entity_id=approval.id,
        event_type="approval_granted",
        severity="success",
        message=f"approved by {actor(user)}: {approval.title}",
        campaign_id=campaign.id,
        campaign_name=campaign.name,
        actor=actor(user),
        payload={
            "reason": approval.reason,
            "prospect_id": approval.prospect_id,
            "message_id": approval.message_id,
            "note": body.note,
            "delivery": delivery,
        },
    )
    await session.commit()

    payload = serialize_approval(approval, campaign.name)
    payload["delivery"] = delivery
    return payload


@router.post("/approvals/{approval_id}/reject")
async def reject(
    approval_id: str, body: DecisionRequest, session: SessionDep, user: UserDep
) -> dict:
    """Reject.

    A rejected *draft* is a note on the writing, so the prospect returns to the
    orchestrator and the next tick writes a new one. A rejected *seniority or
    conflict* gate is a decision about the person, so the prospect is closed —
    re-queuing them would simply raise the identical approval again on the next
    tick, which trains managers to ignore the queue.
    """
    approval = await load_approval(session, approval_id)
    _assert_pending(approval)

    campaign = await session.get(Campaign, approval.campaign_id)
    if campaign is None:
        raise not_found("campaign", approval.campaign_id)

    approval.status = "rejected"
    approval.decided_by_id = user.id
    approval.decided_at = utcnow()
    approval.decision_note = body.note

    message = await session.get(Message, approval.message_id) if approval.message_id else None
    if message is not None:
        message.status = "rejected"
        message.blocked_reason = body.note or f"Rejected by {actor(user)}."

    prospect = await session.get(Prospect, approval.prospect_id) if approval.prospect_id else None
    outcome = "no prospect attached"
    if prospect is not None:
        if approval.reason in TERMINAL_REJECTION_REASONS:
            prospect.status = "closed"
            prospect.next_action_at = None
            outcome = "prospect closed"
        else:
            prospect.status = "pending"
            prospect.next_action_at = utcnow()
            outcome = "prospect returned to the pipeline"
    await session.flush()

    await audit.record(
        session,
        entity_type="approval",
        entity_id=approval.id,
        event_type="approval_rejected",
        severity="warning",
        message=f"rejected by {actor(user)}: {approval.title} ({outcome})",
        campaign_id=campaign.id,
        campaign_name=campaign.name,
        actor=actor(user),
        payload={
            "reason": approval.reason,
            "prospect_id": approval.prospect_id,
            "message_id": approval.message_id,
            "note": body.note,
            "outcome": outcome,
        },
    )
    await session.commit()

    payload = serialize_approval(approval, campaign.name)
    payload["outcome"] = outcome
    return payload


__all__ = ["router"]
