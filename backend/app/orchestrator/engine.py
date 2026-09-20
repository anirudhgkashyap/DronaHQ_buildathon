"""The orchestration engine: the SDR's actual behaviour.

One prospect moves through one pipeline:

    discovered -> researched -> qualified -> contacted -> engaged -> meeting

Each transition is driven by an agent, gated by the guardrail layer, and
recorded in the audit log. The engine is written so that a tick can be
interrupted at any point and safely re-run: every step derives a deterministic
idempotency key from stable identifiers, so a crash between "message created"
and "message sent" resolves on the next tick instead of double-sending.

The reason this is one engine rather than per-channel workers is the thing the
brief asks for: the prospect has one conversation with one SDR, which happens
to be able to reach them on five channels. Channel choice is a decision inside
that conversation, not a separate bot.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit
from ..agents.base import AgentSkipped, make_key, run_agent
from ..agents.schemas import AGENT_LABELS
from ..channels.registry import get_adapter, is_supported
from ..db import new_id
from ..models import (
    Approval,
    Campaign,
    CampaignChannel,
    Company,
    ConversationThread,
    Message,
    Prospect,
    Signal,
    utcnow,
)
from .guardrails import (
    Decision,
    can_send,
    check_campaign_live,
    check_kill_switch,
    consume_rate_limit,
)

logger = logging.getLogger(__name__)


@dataclass
class StepResult:
    """What one unit of orchestration did, for the tick summary and the UI."""

    prospect_id: Optional[str]
    action: str
    detail: str = ""
    blocked_code: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _identity_key(full_name: str, company: Optional[str], email: Optional[str]) -> str:
    """Cross-campaign identity for conflict detection.

    Email when we have one (it is the strongest signal), otherwise a normalised
    name+company pair. Imperfect by nature, which is why a duplicate becomes a
    human-resolvable conflict rather than an automatic drop.
    """
    if email:
        return f"email:{email.strip().lower()}"
    name = "".join(ch for ch in (full_name or "").lower() if ch.isalnum())
    org = "".join(ch for ch in (company or "").lower() if ch.isalnum())
    return f"name:{name}@{org}"


async def _campaign_channels(session: AsyncSession, campaign_id: str) -> list[str]:
    rows = (
        await session.execute(
            select(CampaignChannel).where(
                CampaignChannel.campaign_id == campaign_id,
                CampaignChannel.state == "active",
            )
        )
    ).scalars().all()
    return [r.type for r in rows]


async def _thread_history(session: AsyncSession, prospect_id: str, limit: int = 12) -> list[dict]:
    messages = (
        await session.execute(
            select(Message)
            .where(Message.prospect_id == prospect_id)
            .order_by(Message.created_at.asc())
            .limit(limit)
        )
    ).scalars().all()
    return [
        {
            "direction": m.direction,
            "channel": m.channel,
            "type": m.message_type,
            "subject": m.subject,
            "body": m.body,
            "sent_at": m.sent_at.isoformat() if m.sent_at else None,
        }
        for m in messages
    ]


async def _get_or_create_thread(
    session: AsyncSession, prospect: Prospect, channel: str
) -> ConversationThread:
    """One thread per prospect per channel, so a channel switch is visible in
    the inbox as a related but distinct conversation."""
    thread = (
        await session.execute(
            select(ConversationThread).where(
                ConversationThread.prospect_id == prospect.id,
                ConversationThread.channel == channel,
            )
        )
    ).scalars().first()
    if thread:
        return thread

    thread = ConversationThread(
        id=new_id("thr"),
        campaign_id=prospect.campaign_id,
        prospect_id=prospect.id,
        channel=channel,
        status="active",
    )
    session.add(thread)
    await session.flush()
    return thread


def _prospect_payload(prospect: Prospect) -> dict:
    company = prospect.company
    return {
        "full_name": prospect.full_name,
        "designation": prospect.designation,
        "seniority": prospect.seniority,
        "persona_match": prospect.persona_match,
        "company_name": company.name if company else None,
        "domain": company.domain if company else None,
        "industry": company.industry if company else None,
        "headcount": company.headcount if company else None,
        "hq_location": company.hq_location if company else None,
        "funding_stage": company.funding_stage if company else None,
    }


async def _signal_payload(session: AsyncSession, prospect: Prospect) -> list[dict]:
    signals = (
        await session.execute(
            select(Signal).where(Signal.prospect_id == prospect.id).order_by(Signal.confidence.desc())
        )
    ).scalars().all()
    return [
        {
            "signal_type": s.signal_type,
            "summary": s.summary,
            "source_url": s.source_url,
            "observed_at": s.observed_at.isoformat() if s.observed_at else None,
            "confidence": s.confidence,
        }
        for s in signals
    ]


async def _raise_approval(
    session: AsyncSession,
    campaign: Campaign,
    prospect: Optional[Prospect],
    *,
    title: str,
    reason: str,
    detail: str = "",
    message_id: Optional[str] = None,
) -> Approval:
    """Create a human gate, unless an equivalent one is already open.

    Duplicate approvals for the same cause would train a manager to ignore the
    queue, which defeats the point of having one.
    """
    existing = (
        await session.execute(
            select(Approval).where(
                Approval.campaign_id == campaign.id,
                Approval.prospect_id == (prospect.id if prospect else None),
                Approval.reason == reason,
                Approval.status == "pending",
            )
        )
    ).scalars().first()
    if existing:
        return existing

    approval = Approval(
        id=new_id("apr"),
        campaign_id=campaign.id,
        prospect_id=prospect.id if prospect else None,
        message_id=message_id,
        title=title,
        reason=reason,
        detail=detail,
    )
    session.add(approval)
    if prospect:
        prospect.status = "awaiting_approval"
    await session.flush()

    await audit.record(
        session,
        entity_type="approval",
        entity_id=approval.id,
        event_type="approval_requested",
        severity="warning",
        message=f"needs review: {title}",
        campaign_id=campaign.id,
        campaign_name=campaign.name,
        payload={"reason": reason, "prospect_id": prospect.id if prospect else None},
    )
    return approval


async def _has_open_approval(session: AsyncSession, prospect_id: str) -> bool:
    return bool(
        (
            await session.execute(
                select(Approval.id).where(
                    Approval.prospect_id == prospect_id, Approval.status == "pending"
                )
            )
        ).scalars().first()
    )


# ---------------------------------------------------------------------------
# Stage 1: discovery
# ---------------------------------------------------------------------------
async def discover_prospects(
    session: AsyncSession, campaign: Campaign, *, count: int = 5, batch: Optional[str] = None
) -> list[StepResult]:
    """Run the prospect generation agent and persist new prospects.

    Deduplicated inside the campaign by domain+name, so re-running discovery
    tops the campaign up rather than filling it with copies.
    """
    existing_companies = (
        await session.execute(select(Company.name).where(Company.campaign_id == campaign.id))
    ).scalars().all()

    batch = batch or utcnow().strftime("%Y%m%d%H")
    try:
        run = await run_agent(
            session,
            agent_key="prospect_generation",
            campaign=campaign,
            idempotency_key=make_key("prospect_generation", campaign.id, batch, count),
            variables={
                "count": count,
                "seed_customers": (campaign.icp or {}).get("seed_customers", []),
                "exclusions": list(existing_companies)[:40],
            },
            retrieval_query=f"ideal customer profile and product positioning for {campaign.name}",
            retrieval_types=["icp_definition", "product", "case_study"],
        )
    except AgentSkipped as skip:
        return [StepResult(None, "skipped", skip.decision.reason, skip.decision.code)]

    if run.status != "succeeded":
        return [StepResult(None, "failed", run.error or "prospect generation failed")]

    results: list[StepResult] = []
    for raw in (run.output or {}).get("prospects", []):
        company_name = (raw.get("company_name") or "").strip()
        full_name = (raw.get("full_name") or "").strip()
        if not company_name or not full_name:
            continue

        domain = (raw.get("domain") or "").strip().lower() or None
        company = None
        if domain:
            company = (
                await session.execute(
                    select(Company).where(
                        Company.campaign_id == campaign.id, Company.domain == domain
                    )
                )
            ).scalars().first()
        if company is None:
            company = Company(
                id=new_id("co"),
                campaign_id=campaign.id,
                name=company_name,
                domain=domain,
                industry=raw.get("industry"),
                headcount=raw.get("headcount"),
                hq_location=raw.get("hq_location"),
                funding_stage=raw.get("funding_stage"),
            )
            session.add(company)
            await session.flush()

        duplicate = (
            await session.execute(
                select(Prospect).where(
                    Prospect.campaign_id == campaign.id,
                    Prospect.company_id == company.id,
                    Prospect.full_name == full_name,
                )
            )
        ).scalars().first()
        if duplicate:
            continue

        prospect = Prospect(
            id=new_id("pro"),
            campaign_id=campaign.id,
            company_id=company.id,
            full_name=full_name,
            designation=raw.get("designation"),
            seniority=raw.get("seniority"),
            linkedin_url=raw.get("linkedin_url"),
            email=(raw.get("email") or "").strip().lower() or None,
            persona_match=raw.get("persona_match"),
            source=raw.get("source") or "prospect_generation_agent",
            stage="discovered",
            status="pending",
            identity_key=_identity_key(full_name, company_name, raw.get("email")),
            research={"match_reason": raw.get("match_reason")},
            next_action_at=utcnow(),
        )
        session.add(prospect)
        results.append(StepResult(prospect.id, "discovered", f"{full_name} at {company_name}"))

    await session.flush()
    if results:
        await audit.record_campaign_event(
            session,
            campaign,
            event_type="prospects_discovered",
            message=f"discovered {len(results)} new prospects",
            severity="success",
            payload={"count": len(results)},
        )
    return results


# ---------------------------------------------------------------------------
# Stage 2: research
# ---------------------------------------------------------------------------
async def research_prospect(
    session: AsyncSession, campaign: Campaign, prospect: Prospect
) -> StepResult:
    try:
        run = await run_agent(
            session,
            agent_key="research_enrichment",
            campaign=campaign,
            prospect=prospect,
            idempotency_key=make_key("research_enrichment", prospect.id),
            variables={
                "prospect": _prospect_payload(prospect),
                "signal_types": (campaign.icp or {}).get(
                    "signal_types", ["funding_round", "hiring", "product_launch", "leadership_change"]
                ),
                "freshness_days": (campaign.policies or {}).get("signal_freshness_days", 90),
            },
            retrieval_query=f"{prospect.designation} at {prospect.company.name if prospect.company else ''} research priorities",
            retrieval_types=["icp_definition", "playbook", "product"],
        )
    except AgentSkipped as skip:
        return StepResult(prospect.id, "skipped", skip.decision.reason, skip.decision.code)

    if run.status != "succeeded":
        prospect.next_action_at = utcnow() + dt.timedelta(minutes=30)
        return StepResult(prospect.id, "failed", run.error or "research failed")

    output = run.output or {}
    prospect.research = {
        "company": output.get("company", {}),
        "person": output.get("person", {}),
        "inferred_pain_points": output.get("inferred_pain_points", []),
        "confidence": output.get("confidence"),
        "missing_fields": output.get("missing_fields", []),
        "match_reason": (prospect.research or {}).get("match_reason"),
    }

    channels = output.get("contact_channels") or {}
    prospect.email = prospect.email or (channels.get("email") or "").strip().lower() or None
    prospect.phone = prospect.phone or channels.get("phone")
    prospect.linkedin_url = prospect.linkedin_url or channels.get("linkedin_url")
    if prospect.email and not (prospect.identity_key or "").startswith("email:"):
        prospect.identity_key = _identity_key(prospect.full_name, None, prospect.email)

    if prospect.company:
        company_facts = output.get("company") or {}
        prospect.company.enrichment = company_facts
        prospect.company.industry = prospect.company.industry or company_facts.get("industry")
        prospect.company.headcount = prospect.company.headcount or company_facts.get("headcount")
        prospect.company.funding_stage = prospect.company.funding_stage or company_facts.get("funding_stage")

    for raw in output.get("signals", []) or []:
        observed = raw.get("observed_at")
        try:
            observed_at = dt.datetime.fromisoformat(str(observed).replace("Z", "+00:00")) if observed else None
        except ValueError:
            observed_at = None
        session.add(
            Signal(
                id=new_id("sig"),
                company_id=prospect.company_id,
                prospect_id=prospect.id,
                signal_type=raw.get("signal_type", "unknown"),
                summary=raw.get("summary", ""),
                source_url=raw.get("source_url"),
                observed_at=observed_at,
                confidence=float(raw.get("confidence") or 0.5),
                payload=raw,
            )
        )

    prospect.stage = "researched"
    prospect.status = "pending"
    prospect.next_action_at = utcnow()
    await session.flush()
    return StepResult(prospect.id, "researched", f"{prospect.full_name} enriched")


# ---------------------------------------------------------------------------
# Stage 3: qualification
# ---------------------------------------------------------------------------
async def qualify_prospect(
    session: AsyncSession, campaign: Campaign, prospect: Prospect
) -> StepResult:
    policies = campaign.policies or {}
    try:
        run = await run_agent(
            session,
            agent_key="icp_fit",
            campaign=campaign,
            prospect=prospect,
            idempotency_key=make_key("icp_fit", prospect.id),
            variables={
                "research": prospect.research,
                "prospect": _prospect_payload(prospect),
                "signals": await _signal_payload(session, prospect),
                "qualification_threshold": policies.get("qualification_threshold", 70),
                "borderline_threshold": policies.get("borderline_threshold", 55),
                "exclusions": (campaign.icp or {}).get("exclusions", []),
            },
            retrieval_query=f"ICP qualification criteria and disqualifiers for {campaign.name}",
            retrieval_types=["icp_definition", "playbook"],
        )
    except AgentSkipped as skip:
        return StepResult(prospect.id, "skipped", skip.decision.reason, skip.decision.code)

    if run.status != "succeeded":
        prospect.next_action_at = utcnow() + dt.timedelta(minutes=30)
        return StepResult(prospect.id, "failed", run.error or "qualification failed")

    output = run.output or {}
    prospect.fit_score = float(output.get("score") or 0)
    prospect.fit_verdict = output.get("verdict")
    prospect.fit_rationale = output.get("rationale")
    prospect.fit_criteria = output.get("criteria", {})

    if prospect.fit_verdict == "rejected":
        prospect.status = "rejected"
        prospect.next_action_at = None
        await session.flush()
        await audit.record(
            session,
            entity_type="prospect",
            entity_id=prospect.id,
            event_type="prospect_rejected",
            severity="info",
            message=f"{prospect.full_name} rejected by the ICP gate (score {prospect.fit_score:.0f})",
            campaign_id=campaign.id,
            campaign_name=campaign.name,
            payload={"score": prospect.fit_score, "rationale": prospect.fit_rationale},
            prompt_version_id=run.prompt_version_id,
        )
        return StepResult(prospect.id, "rejected", prospect.fit_rationale or "")

    if prospect.fit_verdict == "borderline":
        # Borderline is the honest answer, and it is exactly the case a human
        # should see rather than have the machine guess.
        await _raise_approval(
            session,
            campaign,
            prospect,
            title=f"{prospect.full_name} scored borderline against the ICP",
            reason="Borderline ICP fit",
            detail=prospect.fit_rationale or "",
        )
        prospect.stage = "researched"
        prospect.next_action_at = None
        await session.flush()
        return StepResult(prospect.id, "escalated", "borderline fit, sent for review")

    prospect.stage = "qualified"
    prospect.status = "pending"
    prospect.next_action_at = utcnow()
    await session.flush()
    await audit.record(
        session,
        entity_type="prospect",
        entity_id=prospect.id,
        event_type="prospect_qualified",
        severity="success",
        message=f"{prospect.full_name} qualified (score {prospect.fit_score:.0f})",
        campaign_id=campaign.id,
        campaign_name=campaign.name,
        payload={"score": prospect.fit_score},
        prompt_version_id=run.prompt_version_id,
    )
    return StepResult(prospect.id, "qualified", f"score {prospect.fit_score:.0f}")


# ---------------------------------------------------------------------------
# Stage 4: outreach
# ---------------------------------------------------------------------------
async def decide_and_send(
    session: AsyncSession, campaign: Campaign, prospect: Prospect
) -> StepResult:
    """Decide the next action, write the message, and send it.

    Three agents cooperate here (strategy, personalisation, then the guardrail
    layer), which is where the "one SDR across channels" behaviour comes from:
    the strategy agent owns channel choice, personalisation owns the words, and
    neither can override policy.
    """
    if await _has_open_approval(session, prospect.id):
        return StepResult(prospect.id, "waiting", "blocked on a pending approval")

    available = await _campaign_channels(session, campaign.id)
    if not available:
        return StepResult(prospect.id, "blocked", "no active channels", "channel_not_configured")

    policies = campaign.policies or {}
    history = await _thread_history(session, prospect.id)

    try:
        strategy_run = await run_agent(
            session,
            agent_key="outreach_strategy",
            campaign=campaign,
            prospect=prospect,
            idempotency_key=make_key(
                "outreach_strategy", prospect.id, len(history), prospect.follow_up_count
            ),
            variables={
                "prospect": _prospect_payload(prospect),
                "qualification": {
                    "score": prospect.fit_score,
                    "verdict": prospect.fit_verdict,
                    "rationale": prospect.fit_rationale,
                },
                "history": history,
                "available_channels": available,
                "channel_priority": policies.get("channel_priority", available),
                "follow_up_count": prospect.follow_up_count,
                "max_follow_ups": policies.get("max_follow_ups", 3),
                "channels_tried": prospect.channels_tried,
            },
            retrieval_query=f"outreach sequencing and channel strategy for {prospect.designation}",
            retrieval_types=["playbook", "example_email"],
        )
    except AgentSkipped as skip:
        return StepResult(prospect.id, "skipped", skip.decision.reason, skip.decision.code)

    if strategy_run.status != "succeeded":
        prospect.next_action_at = utcnow() + dt.timedelta(minutes=30)
        return StepResult(prospect.id, "failed", strategy_run.error or "strategy failed")

    decision = strategy_run.output or {}
    action = decision.get("next_action", "wait")
    channel = decision.get("channel") or (policies.get("channel_priority") or available)[0]

    if action == "wait":
        hours = int(decision.get("wait_hours") or 24)
        prospect.next_action_at = utcnow() + dt.timedelta(hours=hours)
        await session.flush()
        return StepResult(prospect.id, "waiting", f"waiting {hours}h: {decision.get('reason','')}")

    if action == "close":
        prospect.status = "closed"
        prospect.next_action_at = None
        await session.flush()
        await audit.record(
            session,
            entity_type="prospect",
            entity_id=prospect.id,
            event_type="prospect_closed",
            message=f"{prospect.full_name} closed: {decision.get('reason','')}",
            campaign_id=campaign.id,
            campaign_name=campaign.name,
            prompt_version_id=strategy_run.prompt_version_id,
        )
        return StepResult(prospect.id, "closed", decision.get("reason", ""))

    if action == "escalate_human":
        await _raise_approval(
            session,
            campaign,
            prospect,
            title=f"Outreach Strategy Agent escalated {prospect.full_name}",
            reason="Needs human decision",
            detail=decision.get("reason", ""),
        )
        return StepResult(prospect.id, "escalated", decision.get("reason", ""))

    if not is_supported(channel) or channel not in available:
        channel = available[0]

    # --- Seniority gate: contacting very senior people needs a human OK. ----
    seniority_gate = [s.lower() for s in policies.get("require_approval_above_seniority", [])]
    prospect_seniority = (prospect.seniority or prospect.designation or "").lower()
    if any(term in prospect_seniority for term in seniority_gate):
        approval = await _raise_approval(
            session,
            campaign,
            prospect,
            title=f"Outreach Strategy Agent wants to contact a {prospect.designation}",
            reason="Above seniority threshold",
            detail=f"{prospect.full_name} at {prospect.company.name if prospect.company else 'unknown'}",
        )
        return StepResult(prospect.id, "escalated", "seniority gate", "approval_required")

    # --- Guardrail gate before any spend on writing the message. -----------
    gate = await can_send(
        session,
        campaign=campaign,
        prospect=prospect,
        channel=channel,
        skip_frequency=(action == "initial_outreach" and prospect.last_contacted_at is None),
    )
    if not gate.allowed:
        if gate.retry_after:
            prospect.next_action_at = gate.retry_after
        elif gate.code in {"suppressed", "duplicate_outreach"}:
            prospect.status = "closed" if gate.code == "suppressed" else prospect.status
            prospect.next_action_at = None
        await session.flush()

        if gate.code == "duplicate_outreach":
            await _raise_approval(
                session,
                campaign,
                prospect,
                title=f"{prospect.full_name} is also being contacted by {gate.context.get('conflicting_campaign_name')}",
                reason="Conflict",
                detail=gate.reason,
            )
        await audit.record(
            session,
            entity_type="prospect",
            entity_id=prospect.id,
            event_type="outreach_blocked",
            severity="warning",
            message=f"outreach to {prospect.full_name} blocked: {gate.reason}",
            campaign_id=campaign.id,
            campaign_name=campaign.name,
            payload={"code": gate.code, "channel": channel},
        )
        return StepResult(prospect.id, "blocked", gate.reason, gate.code)

    # --- Write the message. ------------------------------------------------
    message_type = "follow_up" if action == "follow_up" else "initial_outreach"
    sequence = len([m for m in history if m["direction"] == "outbound"])
    idem = make_key("message", prospect.id, channel, message_type, sequence)

    existing_message = (
        await session.execute(select(Message).where(Message.idempotency_key == idem))
    ).scalar_one_or_none()
    if existing_message and existing_message.status in {"sent", "delivered", "replied"}:
        return StepResult(prospect.id, "noop", "message already sent")

    try:
        writer_run = await run_agent(
            session,
            agent_key="personalisation",
            campaign=campaign,
            prospect=prospect,
            idempotency_key=make_key("personalisation", prospect.id, channel, message_type, sequence),
            variables={
                "channel": channel,
                "message_goal": decision.get("message_goal", "start a conversation"),
                "brief": decision.get("brief", ""),
                "prospect": _prospect_payload(prospect),
                "research": prospect.research,
                "signals": await _signal_payload(session, prospect),
                "history": history,
                "message_type": message_type,
            },
            retrieval_query=(
                f"{decision.get('message_goal','outreach')} for {prospect.designation} "
                f"in {prospect.company.industry if prospect.company else 'their industry'}"
            ),
            retrieval_types=["product", "case_study", "example_email", "objection_handling"],
            retrieval_limit=6,
        )
    except AgentSkipped as skip:
        return StepResult(prospect.id, "skipped", skip.decision.reason, skip.decision.code)

    if writer_run.status != "succeeded":
        prospect.next_action_at = utcnow() + dt.timedelta(minutes=30)
        return StepResult(prospect.id, "failed", writer_run.error or "personalisation failed")

    draft = writer_run.output or {}
    confidence = float(draft.get("confidence") or 0)
    thread = await _get_or_create_thread(session, prospect, channel)

    message = existing_message or Message(id=new_id("msg"), idempotency_key=idem)
    message.thread_id = thread.id
    message.campaign_id = campaign.id
    message.prospect_id = prospect.id
    message.channel = channel
    message.direction = "outbound"
    message.message_type = message_type
    message.subject = draft.get("subject")
    message.body = draft.get("body", "")
    message.personalisation_evidence = draft.get("personalisation_evidence", [])
    message.confidence = confidence
    message.prompt_version_id = writer_run.prompt_version_id
    message.agent_run_id = writer_run.id
    message.cost_usd = (writer_run.cost_usd or 0) + (strategy_run.cost_usd or 0)
    if existing_message is None:
        session.add(message)
    await session.flush()

    # --- Confidence gate: a weak draft goes to a human, not to a prospect. --
    min_confidence = float(policies.get("require_approval_below_confidence", 0.6))
    if confidence < min_confidence:
        message.status = "pending_approval"
        await _raise_approval(
            session,
            campaign,
            prospect,
            title=f"Personalisation Agent's draft for {prospect.full_name} scored low confidence",
            reason="Low confidence",
            detail=f"Confidence {confidence:.2f} is below the campaign threshold of {min_confidence:.2f}.",
            message_id=message.id,
        )
        await session.flush()
        return StepResult(prospect.id, "escalated", "low confidence draft")

    return await deliver_message(session, campaign, prospect, message)


async def deliver_message(
    session: AsyncSession, campaign: Campaign, prospect: Prospect, message: Message
) -> StepResult:
    """Hand a message to its channel adapter and record the outcome.

    Re-checks the guardrails immediately before the external call, because an
    operator may have hit the kill switch while the message was being written.
    """
    gate = await can_send(
        session, campaign=campaign, prospect=prospect, channel=message.channel, skip_frequency=True
    )
    if not gate.allowed:
        message.status = "blocked"
        message.blocked_reason = gate.reason
        await session.flush()
        return StepResult(prospect.id, "blocked", gate.reason, gate.code)

    adapter = get_adapter(message.channel)
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
        prospect.current_channel = message.channel
        if message.channel not in (prospect.channels_tried or []):
            prospect.channels_tried = list(prospect.channels_tried or []) + [message.channel]
        if message.message_type == "follow_up":
            prospect.follow_up_count += 1
        if prospect.stage in {"qualified", "researched", "discovered"}:
            prospect.stage = "contacted"
        prospect.status = "in_outreach"
        prospect.next_action_at = utcnow() + dt.timedelta(
            hours=int((campaign.policies or {}).get("follow_up_gap_hours", 72))
        )

        thread = await session.get(ConversationThread, message.thread_id)
        if thread:
            thread.status = "awaiting_reply"
            thread.last_activity_at = utcnow()

        await consume_rate_limit(
            session, scope="campaign_channel", scope_key=f"{campaign.id}:{message.channel}"
        )
        await consume_rate_limit(session, scope="campaign", scope_key=campaign.id)

        await audit.record(
            session,
            entity_type="message",
            entity_id=message.id,
            event_type="message_sent",
            severity="success",
            message=(
                f"{message.message_type.replace('_', ' ')} sent to {prospect.full_name} "
                f"on {message.channel}"
            ),
            campaign_id=campaign.id,
            campaign_name=campaign.name,
            payload={
                "channel": message.channel,
                "prospect_id": prospect.id,
                "dry_run": result.dry_run,
                "confidence": message.confidence,
            },
            prompt_version_id=message.prompt_version_id,
        )
        await session.flush()
        return StepResult(prospect.id, "sent", f"{message.channel} -> {prospect.full_name}")

    prospect.next_action_at = utcnow() + dt.timedelta(minutes=45)
    await session.flush()
    await audit.record(
        session,
        entity_type="message",
        entity_id=message.id,
        event_type="message_failed",
        severity="error",
        message=f"{message.channel} send to {prospect.full_name} failed: {result.error}",
        campaign_id=campaign.id,
        campaign_name=campaign.name,
        payload={"channel": message.channel, "error": result.error},
    )
    return StepResult(prospect.id, "failed", result.error or "send failed")


# ---------------------------------------------------------------------------
# Stage 5: replies
# ---------------------------------------------------------------------------
async def handle_inbound(
    session: AsyncSession,
    campaign: Campaign,
    prospect: Prospect,
    *,
    channel: str,
    body: str,
    external_id: Optional[str] = None,
) -> StepResult:
    """Record an inbound reply and let the conversation agent decide next.

    Inbound is recorded even when the campaign is paused or the kill switch is
    engaged: refusing to *listen* would lose data, and only outbound action is
    actually dangerous.
    """
    thread = await _get_or_create_thread(session, prospect, channel)
    idem = make_key("inbound", prospect.id, channel, external_id or body[:40])

    existing = (
        await session.execute(select(Message).where(Message.idempotency_key == idem))
    ).scalar_one_or_none()
    if existing:
        return StepResult(prospect.id, "noop", "reply already recorded")

    inbound = Message(
        id=new_id("msg"),
        thread_id=thread.id,
        campaign_id=campaign.id,
        prospect_id=prospect.id,
        channel=channel,
        direction="inbound",
        message_type="reply",
        body=body,
        status="delivered",
        idempotency_key=idem,
        external_id=external_id,
        sent_at=utcnow(),
    )
    session.add(inbound)
    thread.status = "active"
    thread.last_activity_at = utcnow()
    prospect.status = "replied"
    prospect.stage = "engaged" if prospect.stage in {"contacted", "qualified"} else prospect.stage
    await session.flush()

    await audit.record(
        session,
        entity_type="message",
        entity_id=inbound.id,
        event_type="reply_received",
        severity="info",
        message=f"{prospect.full_name} replied on {channel}",
        campaign_id=campaign.id,
        campaign_name=campaign.name,
        payload={"channel": channel, "prospect_id": prospect.id},
    )

    history = await _thread_history(session, prospect.id)
    try:
        run = await run_agent(
            session,
            agent_key="conversation",
            campaign=campaign,
            prospect=prospect,
            idempotency_key=make_key("conversation", prospect.id, inbound.id),
            variables={
                "reply": body,
                "history": history,
                "prospect": _prospect_payload(prospect),
                "channel": channel,
            },
            retrieval_query=f"objection handling and replies for: {body[:200]}",
            retrieval_types=["objection_handling", "playbook", "example_email"],
        )
    except AgentSkipped as skip:
        # The reply is safely stored; deciding what to do waits for resume.
        return StepResult(prospect.id, "recorded", skip.decision.reason, skip.decision.code)

    if run.status != "succeeded":
        return StepResult(prospect.id, "recorded", "reply stored, analysis failed")

    verdict = run.output or {}
    thread.sentiment = verdict.get("sentiment")
    next_action = verdict.get("next_action")
    intent = verdict.get("intent")

    if intent == "unsubscribe":
        from ..models import SuppressionEntry

        session.add(
            SuppressionEntry(
                id=new_id("sup"),
                email=prospect.email,
                phone=prospect.phone,
                linkedin_url=prospect.linkedin_url,
                reason="unsubscribed",
                scope="global",
            )
        )
        prospect.status = "closed"
        prospect.next_action_at = None
        thread.status = "closed"
        await session.flush()
        await audit.record(
            session,
            entity_type="prospect",
            entity_id=prospect.id,
            event_type="opted_out",
            severity="warning",
            message=f"{prospect.full_name} opted out and was added to the do-not-contact list",
            campaign_id=campaign.id,
            campaign_name=campaign.name,
        )
        return StepResult(prospect.id, "opted_out", "added to suppression list")

    if next_action == "book_meeting":
        prospect.stage = "meeting"
        prospect.status = "meeting_booked"
        prospect.next_action_at = None
        thread.status = "meeting_booked"
        await session.flush()
        await audit.record(
            session,
            entity_type="prospect",
            entity_id=prospect.id,
            event_type="meeting_booked",
            severity="success",
            message=f"meeting booked with {prospect.full_name}",
            campaign_id=campaign.id,
            campaign_name=campaign.name,
            prompt_version_id=run.prompt_version_id,
        )
        return StepResult(prospect.id, "meeting_booked", prospect.full_name)

    if next_action == "escalate_human":
        thread.status = "escalated"
        thread.escalation_reason = verdict.get("escalation_reason")
        prospect.status = "escalated"
        await _raise_approval(
            session,
            campaign,
            prospect,
            title=f"Conversation Agent flagged {verdict.get('intent','a reply')} from {prospect.full_name}",
            reason="Needs human reply",
            detail=verdict.get("escalation_reason") or body[:400],
        )
        return StepResult(prospect.id, "escalated", verdict.get("escalation_reason") or "")

    if next_action == "close":
        prospect.status = "closed"
        prospect.next_action_at = None
        thread.status = "closed"
        await session.flush()
        return StepResult(prospect.id, "closed", intent or "")

    # reply / follow_up: queue the next outbound turn.
    prospect.status = "in_outreach"
    prospect.next_action_at = utcnow()
    await session.flush()
    return StepResult(prospect.id, "queued_reply", verdict.get("suggested_reply", "")[:120])


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
STAGE_HANDLERS = {
    "discovered": research_prospect,
    "researched": qualify_prospect,
}


async def advance_prospect(
    session: AsyncSession, campaign: Campaign, prospect: Prospect
) -> StepResult:
    """Move one prospect one step. Which step depends on where it is."""
    if prospect.status in {"rejected", "closed", "meeting_booked"}:
        return StepResult(prospect.id, "noop", "terminal state")
    if prospect.status == "awaiting_approval" or await _has_open_approval(session, prospect.id):
        return StepResult(prospect.id, "waiting", "pending approval")

    handler = STAGE_HANDLERS.get(prospect.stage)
    if handler:
        return await handler(session, campaign, prospect)
    return await decide_and_send(session, campaign, prospect)


async def tick_campaign(
    session: AsyncSession, campaign: Campaign, *, limit: Optional[int] = None
) -> list[StepResult]:
    """One unit of autonomous work for one campaign.

    Returns early on a blocked campaign so a paused campaign costs nothing,
    which is what lets many campaigns run side by side.
    """
    from ..config import settings as cfg

    limit = limit or cfg.MAX_PROSPECTS_PER_TICK

    kill = await check_kill_switch(session)
    if not kill.allowed:
        return [StepResult(None, "halted", kill.reason, kill.code)]

    live = check_campaign_live(campaign)
    if not live.allowed:
        return [StepResult(None, "skipped", live.reason, live.code)]

    now = utcnow()
    due = (
        await session.execute(
            select(Prospect)
            .where(
                Prospect.campaign_id == campaign.id,
                Prospect.status.notin_(["rejected", "closed", "meeting_booked", "awaiting_approval"]),
                (Prospect.next_action_at.is_(None)) | (Prospect.next_action_at <= now),
            )
            .order_by(Prospect.next_action_at.asc().nullsfirst())
            .limit(limit)
        )
    ).scalars().all()

    results: list[StepResult] = []
    for prospect in due:
        try:
            results.append(await advance_prospect(session, campaign, prospect))
        except Exception as exc:  # one bad prospect must not stop the campaign
            logger.exception("prospect %s failed", prospect.id)
            results.append(StepResult(prospect.id, "error", str(exc)))

    # Top the campaign up when it is running dry, so a live campaign keeps
    # working instead of quietly finishing its seed list.
    if len(due) < limit:
        active = (
            await session.execute(
                select(func.count(Prospect.id)).where(
                    Prospect.campaign_id == campaign.id,
                    Prospect.status.notin_(["rejected", "closed"]),
                )
            )
        ).scalar() or 0
        target = int((campaign.icp or {}).get("target_prospect_count", 25))
        if active < target:
            results.extend(await discover_prospects(session, campaign, count=min(5, target - active)))

    return results
