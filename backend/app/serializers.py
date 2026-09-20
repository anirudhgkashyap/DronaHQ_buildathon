"""Model -> JSON serializers.

These produce exactly the shapes documented in frontend/README.md. Keeping
them in one file means the API contract can be verified by reading a single
module instead of chasing response models through every router.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from .models import (
    Approval,
    AuditLog,
    Campaign,
    ConversationThread,
    KnowledgeChunk,
    Message,
    Prospect,
    PromptVersion,
    AgentConfig,
    User,
)


def iso(value: Optional[dt.datetime]) -> Optional[str]:
    """ISO 8601 UTC with a trailing Z, which is what the frontend parses."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def user_ref(user: Optional[User]) -> Optional[dict]:
    return {"id": user.id, "name": user.name} if user else None


def serialize_user(user: User) -> dict:
    return {
        "id": user.id,
        "name": user.name,
        "initials": user.initials,
        "role": user.role,
        "email": user.email,
        "active": user.active,
        "channels_available": user.channels_available,
        "working_hours": user.working_hours,
        "daily_activity_limit": user.daily_activity_limit,
    }


def serialize_campaign(
    campaign: Campaign,
    metrics: Optional[dict] = None,
    pending_approvals: int = 0,
) -> dict:
    """The campaign list-item shape. Every campaign endpoint returns this."""
    return {
        "id": campaign.id,
        "name": campaign.name,
        "label": campaign.label,
        "description": campaign.description,
        "icp_summary": campaign.icp_summary,
        "status": campaign.status,
        "owner": user_ref(campaign.owner),
        "channels": [
            {"type": ch.type, "state": ch.state, "daily_limit": ch.daily_limit}
            for ch in sorted(campaign.channels, key=lambda c: c.type)
        ],
        "metrics": metrics or {"prospects": 0, "outreach": 0, "meetings": 0},
        "pending_approvals": pending_approvals,
        "created_at": iso(campaign.created_at),
        "updated_at": iso(campaign.updated_at),
        "paused_at": iso(campaign.paused_at),
        "paused_by": user_ref(campaign.paused_by),
        "variant_of": campaign.variant_of,
        "duplicated_from": campaign.duplicated_from,
    }


def serialize_campaign_detail(
    campaign: Campaign,
    metrics: dict,
    funnel: dict,
    pending_approvals: int,
    agent_activity: dict,
    outcomes: dict,
    reps: list,
) -> dict:
    base = serialize_campaign(campaign, metrics, pending_approvals)
    base.update(
        {
            "icp": campaign.icp,
            "product_context": campaign.product_context,
            "objective": campaign.objective,
            "policies": campaign.policies,
            "funnel": funnel,
            "agent_activity": agent_activity,
            "outcomes": outcomes,
            "reps": reps,
        }
    )
    return base


def serialize_prospect(prospect: Prospect) -> dict:
    company = prospect.company
    return {
        "id": prospect.id,
        "campaign_id": prospect.campaign_id,
        "full_name": prospect.full_name,
        "designation": prospect.designation,
        "seniority": prospect.seniority,
        "linkedin_url": prospect.linkedin_url,
        "email": prospect.email,
        "phone": prospect.phone,
        "persona_match": prospect.persona_match,
        "source": prospect.source,
        "stage": prospect.stage,
        "status": prospect.status,
        "fit_score": prospect.fit_score,
        "fit_verdict": prospect.fit_verdict,
        "fit_rationale": prospect.fit_rationale,
        "fit_criteria": prospect.fit_criteria,
        "research": prospect.research,
        "follow_up_count": prospect.follow_up_count,
        "current_channel": prospect.current_channel,
        "channels_tried": prospect.channels_tried,
        "last_contacted_at": iso(prospect.last_contacted_at),
        "next_action_at": iso(prospect.next_action_at),
        "created_at": iso(prospect.created_at),
        "company": (
            {
                "id": company.id,
                "name": company.name,
                "domain": company.domain,
                "industry": company.industry,
                "headcount": company.headcount,
                "hq_location": company.hq_location,
                "funding_stage": company.funding_stage,
            }
            if company
            else None
        ),
    }


def serialize_message(message: Message) -> dict:
    return {
        "id": message.id,
        "thread_id": message.thread_id,
        "campaign_id": message.campaign_id,
        "prospect_id": message.prospect_id,
        "channel": message.channel,
        "direction": message.direction,
        "message_type": message.message_type,
        "subject": message.subject,
        "body": message.body,
        "status": message.status,
        "blocked_reason": message.blocked_reason,
        "error": message.error,
        "confidence": message.confidence,
        "personalisation_evidence": message.personalisation_evidence,
        "prompt_version_id": message.prompt_version_id,
        "cost_usd": round(message.cost_usd or 0.0, 6),
        "created_at": iso(message.created_at),
        "sent_at": iso(message.sent_at),
    }


def serialize_thread(thread: ConversationThread, last_message: Optional[Message] = None) -> dict:
    prospect = thread.prospect
    return {
        "id": thread.id,
        "campaign_id": thread.campaign_id,
        "prospect_id": thread.prospect_id,
        "prospect_name": prospect.full_name if prospect else None,
        "prospect_designation": prospect.designation if prospect else None,
        "company_name": prospect.company.name if prospect and prospect.company else None,
        "channel": thread.channel,
        "status": thread.status,
        "sentiment": thread.sentiment,
        "escalation_reason": thread.escalation_reason,
        "last_activity_at": iso(thread.last_activity_at),
        "preview": (last_message.body[:160] if last_message else None),
        "last_direction": last_message.direction if last_message else None,
    }


def serialize_approval(approval: Approval, campaign_name: Optional[str] = None) -> dict:
    return {
        "id": approval.id,
        "campaign_id": approval.campaign_id,
        "campaign_name": campaign_name,
        "prospect_id": approval.prospect_id,
        "message_id": approval.message_id,
        "title": approval.title,
        "reason": approval.reason,
        "detail": approval.detail,
        "status": approval.status,
        "created_at": iso(approval.created_at),
        "decided_at": iso(approval.decided_at),
    }


def serialize_event(entry: AuditLog) -> dict:
    """Audit rows become the activity feed. The frontend renders
    ``campaign_name`` in bold followed by ``message``."""
    return {
        "id": f"evt_{entry.id}",
        "type": entry.severity,
        "campaign_name": entry.payload.get("campaign_name"),
        "message": entry.message,
        "actor": entry.actor,
        "entity_type": entry.entity_type,
        "entity_id": entry.entity_id,
        "created_at": iso(entry.created_at),
    }


def serialize_prompt_version(version: PromptVersion) -> dict:
    return {
        "id": version.id,
        "campaign_id": version.campaign_id,
        "agent_key": version.agent_key,
        "version": version.version,
        "content": version.content,
        "notes": version.notes,
        "is_active": version.is_active,
        "created_by": user_ref(version.created_by),
        "created_at": iso(version.created_at),
        "activated_at": iso(version.activated_at),
    }


def serialize_agent_config(config: AgentConfig, stats: Optional[dict] = None) -> dict:
    return {
        "id": config.id,
        "campaign_id": config.campaign_id,
        "agent_key": config.agent_key,
        "enabled": config.enabled,
        "state": config.state,
        "model": config.model,
        "tools": config.tools,
        "thresholds": config.thresholds,
        "escalation_rules": config.escalation_rules,
        "dronahq_agent_id": config.dronahq_agent_id,
        "stats": stats or {},
        "updated_at": iso(config.updated_at),
    }


def serialize_chunk(chunk: KnowledgeChunk, score: Optional[float] = None) -> dict:
    payload: dict[str, Any] = {
        "id": chunk.id,
        "document_id": chunk.document_id,
        "campaign_id": chunk.campaign_id,
        "doc_type": chunk.doc_type,
        "title": chunk.title,
        "content": chunk.content,
        "meta": chunk.meta,
    }
    if score is not None:
        payload["score"] = round(score, 4)
    return payload
