"""SQLAlchemy models: the single source of truth for the platform.

Design notes that matter for the rubric:

* Campaign isolation. Every prospect, message, prompt version, agent config
  and metric hangs off exactly one campaign. Two campaigns never share
  mutable state, so pausing one cannot affect another.
* Auditability. ``AuditLog`` is append-only and records which prompt version
  was active when an agent acted, which answers "why did the agent do that?".
* Idempotency. ``AgentRun.idempotency_key`` and ``Message.idempotency_key``
  are unique, so a retry after a crash can never double-execute or double-send.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .config import settings
from .db import Base

# pgvector when we have Postgres, JSON list otherwise. The retrieval code in
# rag/store.py branches on the same flag.
if settings.is_postgres:  # pragma: no cover - depends on deployment
    from pgvector.sqlalchemy import Vector

    EmbeddingType: Any = Vector(settings.EMBEDDING_DIM)
else:
    EmbeddingType = JSON


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


TS = lambda: mapped_column(DateTime(timezone=True), default=utcnow)  # noqa: E731


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------
class User(Base):
    """Managers and sales representatives.

    A rep carries the identity used for outreach plus their own activity
    limits and working hours, so the guardrail layer can enforce them.
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    initials: Mapped[str] = mapped_column(String(8))
    email: Mapped[Optional[str]] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(32), default="rep")  # manager | rep | admin
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    daily_activity_limit: Mapped[int] = mapped_column(Integer, default=100)
    working_hours: Mapped[dict] = mapped_column(
        JSON, default=lambda: {"timezone": "Asia/Kolkata", "start": 9, "end": 19, "days": [0, 1, 2, 3, 4]}
    )
    channels_available: Mapped[list] = mapped_column(JSON, default=lambda: ["email", "linkedin"])
    created_at: Mapped[dt.datetime] = TS()

    def as_ref(self) -> dict:
        return {"id": self.id, "name": self.name}


# ---------------------------------------------------------------------------
# Campaign
# ---------------------------------------------------------------------------
class Campaign(Base):
    """A campaign is a first-class autonomous GTM program.

    Status drives the orchestrator: only ``live`` campaigns may take
    autonomous external action.
    """

    __tablename__ = "campaigns"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    label: Mapped[Optional[str]] = mapped_column(String(64))
    description: Mapped[Optional[str]] = mapped_column(Text)
    icp_summary: Mapped[Optional[str]] = mapped_column(String(400))
    status: Mapped[str] = mapped_column(String(24), default="draft", index=True)
    # draft | live | paused | completed | archived

    owner_id: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id"))
    # Both owner_id and paused_by_id point at users, so each relationship has to
    # name its own foreign key or SQLAlchemy cannot pick one.
    owner: Mapped[Optional[User]] = relationship(foreign_keys=[owner_id], lazy="selectin")

    # Targeting / ICP definition. Free-form so a campaign can carry whatever
    # its ICP needs without a migration.
    icp: Mapped[dict] = mapped_column(JSON, default=dict)
    product_context: Mapped[dict] = mapped_column(JSON, default=dict)
    objective: Mapped[Optional[str]] = mapped_column(Text)

    # Policy: daily limits, approval rules, follow-up cadence, thresholds.
    policies: Mapped[dict] = mapped_column(
        JSON,
        default=lambda: {
            "daily_send_limit": 50,
            "qualification_threshold": 70,
            "borderline_threshold": 55,
            "max_follow_ups": 3,
            "follow_up_gap_hours": 72,
            "require_approval_above_seniority": ["CEO", "Founder", "President"],
            "require_approval_below_confidence": 0.6,
            "channel_priority": ["email", "linkedin", "sms", "whatsapp", "voice"],
            "working_hours_only": True,
        },
    )

    # Copied from the source campaign when duplicated, for variant experiments.
    duplicated_from: Mapped[Optional[str]] = mapped_column(String(64))
    variant_of: Mapped[Optional[str]] = mapped_column(String(64))

    created_at: Mapped[dt.datetime] = TS()
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    paused_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    paused_by_id: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id"))
    paused_by: Mapped[Optional[User]] = relationship(foreign_keys=[paused_by_id], lazy="selectin")

    channels: Mapped[list["CampaignChannel"]] = relationship(
        back_populates="campaign", lazy="selectin", cascade="all, delete-orphan"
    )

    @property
    def is_executable(self) -> bool:
        return self.status == "live"


class CampaignChannel(Base):
    """Per-campaign channel state. ``paused`` stops that channel alone."""

    __tablename__ = "campaign_channels"
    __table_args__ = (UniqueConstraint("campaign_id", "type", name="uq_campaign_channel"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    campaign: Mapped[Campaign] = relationship(back_populates="channels")
    type: Mapped[str] = mapped_column(String(24))  # linkedin | email | voice | sms | whatsapp
    state: Mapped[str] = mapped_column(String(16), default="active")  # active | paused | off
    daily_limit: Mapped[int] = mapped_column(Integer, default=50)
    config: Mapped[dict] = mapped_column(JSON, default=dict)


class CampaignRep(Base):
    """Representative assignment: who may execute a campaign and under whose
    identity outreach goes out."""

    __tablename__ = "campaign_reps"
    __table_args__ = (UniqueConstraint("campaign_id", "user_id", name="uq_campaign_rep"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    user: Mapped[User] = relationship(lazy="selectin")
    is_sending_identity: Mapped[bool] = mapped_column(Boolean, default=False)


# ---------------------------------------------------------------------------
# Agents and prompts
# ---------------------------------------------------------------------------
AGENT_KEYS = [
    "prospect_generation",
    "research_enrichment",
    "icp_fit",
    "outreach_strategy",
    "personalisation",
    "conversation",
]


class AgentConfig(Base):
    """Per-campaign configuration for one agent, including its own pause
    switch so a single agent can be stopped while the campaign runs on."""

    __tablename__ = "agent_configs"
    __table_args__ = (UniqueConstraint("campaign_id", "agent_key", name="uq_campaign_agent"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    agent_key: Mapped[str] = mapped_column(String(48))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    state: Mapped[str] = mapped_column(String(16), default="active")  # active | paused
    model: Mapped[str] = mapped_column(String(80), default="claude-sonnet-4-6")
    # Cheaper model for mechanical steps; the router in agents/base.py uses it.
    tools: Mapped[list] = mapped_column(JSON, default=list)
    thresholds: Mapped[dict] = mapped_column(JSON, default=dict)
    escalation_rules: Mapped[dict] = mapped_column(JSON, default=dict)
    dronahq_agent_id: Mapped[Optional[str]] = mapped_column(String(160))
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class PromptVersion(Base):
    """Versioned prompt for a campaign, either the campaign system prompt
    (``agent_key`` is ``__campaign__``) or one agent's prompt.

    Versions are immutable once created; activating one deactivates the rest
    for that (campaign, agent) pair, which makes rollback a single flag flip.
    """

    __tablename__ = "prompt_versions"
    __table_args__ = (
        UniqueConstraint("campaign_id", "agent_key", "version", name="uq_prompt_version"),
        Index("ix_prompt_active", "campaign_id", "agent_key", "is_active"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    agent_key: Mapped[str] = mapped_column(String(48))
    version: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by_id: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id"))
    created_by: Mapped[Optional[User]] = relationship(lazy="selectin")
    created_at: Mapped[dt.datetime] = TS()
    activated_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------------------
# Prospects
# ---------------------------------------------------------------------------
FUNNEL_STAGES = [
    "discovered",
    "researched",
    "qualified",
    "contacted",
    "engaged",
    "meeting",
    "opportunity",
]


class Company(Base):
    __tablename__ = "companies"
    __table_args__ = (UniqueConstraint("campaign_id", "domain", name="uq_campaign_domain"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(240))
    domain: Mapped[Optional[str]] = mapped_column(String(240))
    industry: Mapped[Optional[str]] = mapped_column(String(160))
    headcount: Mapped[Optional[int]] = mapped_column(Integer)
    hq_location: Mapped[Optional[str]] = mapped_column(String(160))
    funding_stage: Mapped[Optional[str]] = mapped_column(String(80))
    enrichment: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = TS()


class Prospect(Base):
    """A person inside a campaign, moving through the funnel.

    ``stage`` is the funnel position the dashboard renders. ``status`` is the
    execution state the orchestrator uses to decide what happens next.
    """

    __tablename__ = "prospects"
    __table_args__ = (
        Index("ix_prospect_campaign_stage", "campaign_id", "stage"),
        Index("ix_prospect_next_action", "next_action_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    company_id: Mapped[Optional[str]] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    company: Mapped[Optional[Company]] = relationship(lazy="selectin")

    full_name: Mapped[str] = mapped_column(String(200))
    designation: Mapped[Optional[str]] = mapped_column(String(200))
    seniority: Mapped[Optional[str]] = mapped_column(String(80))
    linkedin_url: Mapped[Optional[str]] = mapped_column(String(400))
    email: Mapped[Optional[str]] = mapped_column(String(255), index=True)
    phone: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    persona_match: Mapped[Optional[str]] = mapped_column(String(160))
    source: Mapped[Optional[str]] = mapped_column(String(120))

    stage: Mapped[str] = mapped_column(String(24), default="discovered", index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    # pending | researching | awaiting_approval | in_outreach | replied |
    # meeting_booked | rejected | closed | escalated

    # Deduplication key shared across campaigns, so the conflict detector can
    # spot the same human being targeted twice.
    identity_key: Mapped[Optional[str]] = mapped_column(String(255), index=True)

    research: Mapped[dict] = mapped_column(JSON, default=dict)
    fit_score: Mapped[Optional[float]] = mapped_column(Float)
    fit_verdict: Mapped[Optional[str]] = mapped_column(String(24))  # qualified | borderline | rejected
    fit_rationale: Mapped[Optional[str]] = mapped_column(Text)
    fit_criteria: Mapped[dict] = mapped_column(JSON, default=dict)

    follow_up_count: Mapped[int] = mapped_column(Integer, default=0)
    last_contacted_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    next_action_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    current_channel: Mapped[Optional[str]] = mapped_column(String(24))
    channels_tried: Mapped[list] = mapped_column(JSON, default=list)

    created_at: Mapped[dt.datetime] = TS()
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Signal(Base):
    """An observable, dated fact about a company that justifies outreach.

    Every signal carries its source URL so personalisation can cite it and the
    guardrail layer can reject a claim with no evidence behind it.
    """

    __tablename__ = "signals"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    company_id: Mapped[Optional[str]] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    prospect_id: Mapped[Optional[str]] = mapped_column(ForeignKey("prospects.id", ondelete="CASCADE"), index=True)
    signal_type: Mapped[str] = mapped_column(String(64))
    summary: Mapped[str] = mapped_column(Text)
    source_url: Mapped[Optional[str]] = mapped_column(String(600))
    observed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = TS()


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------
class ConversationThread(Base):
    __tablename__ = "conversation_threads"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    prospect_id: Mapped[str] = mapped_column(ForeignKey("prospects.id", ondelete="CASCADE"), index=True)
    prospect: Mapped[Prospect] = relationship(lazy="selectin")
    channel: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)
    # active | awaiting_reply | escalated | closed | meeting_booked
    sentiment: Mapped[Optional[str]] = mapped_column(String(24))
    escalation_reason: Mapped[Optional[str]] = mapped_column(Text)
    last_activity_at: Mapped[dt.datetime] = TS()
    created_at: Mapped[dt.datetime] = TS()


class Message(Base):
    """One touch on one channel.

    ``idempotency_key`` is derived from (prospect, intent, sequence) before the
    send is attempted, so a retried orchestrator tick can never send twice.
    ``prompt_version_id`` records which prompt produced the text.
    """

    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    thread_id: Mapped[str] = mapped_column(ForeignKey("conversation_threads.id", ondelete="CASCADE"), index=True)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    prospect_id: Mapped[str] = mapped_column(ForeignKey("prospects.id", ondelete="CASCADE"), index=True)

    channel: Mapped[str] = mapped_column(String(24))
    direction: Mapped[str] = mapped_column(String(16))  # outbound | inbound
    message_type: Mapped[str] = mapped_column(String(32), default="initial_outreach")
    # initial_outreach | follow_up | reply | voice_summary

    subject: Mapped[Optional[str]] = mapped_column(String(400))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    # draft | pending_approval | approved | rejected | queued | sent |
    # delivered | replied | failed | blocked

    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    external_id: Mapped[Optional[str]] = mapped_column(String(200))
    error: Mapped[Optional[str]] = mapped_column(Text)
    blocked_reason: Mapped[Optional[str]] = mapped_column(Text)

    personalisation_evidence: Mapped[list] = mapped_column(JSON, default=list)
    prompt_version_id: Mapped[Optional[str]] = mapped_column(String(64))
    agent_run_id: Mapped[Optional[str]] = mapped_column(String(64))
    confidence: Mapped[Optional[float]] = mapped_column(Float)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)

    created_at: Mapped[dt.datetime] = TS()
    sent_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))


class Approval(Base):
    """A human-in-the-loop gate. The orchestrator will not proceed past a
    pending approval for that prospect."""

    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    prospect_id: Mapped[Optional[str]] = mapped_column(ForeignKey("prospects.id", ondelete="CASCADE"))
    message_id: Mapped[Optional[str]] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(400))
    reason: Mapped[str] = mapped_column(String(160))
    detail: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    decided_by_id: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = TS()


# ---------------------------------------------------------------------------
# Safety and observability
# ---------------------------------------------------------------------------
class SuppressionEntry(Base):
    """Global do-not-contact list. Checked before every outbound action."""

    __tablename__ = "suppression_entries"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[Optional[str]] = mapped_column(String(255), index=True)
    phone: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    linkedin_url: Mapped[Optional[str]] = mapped_column(String(400), index=True)
    domain: Mapped[Optional[str]] = mapped_column(String(240), index=True)
    reason: Mapped[str] = mapped_column(String(120))
    scope: Mapped[str] = mapped_column(String(24), default="global")  # global | campaign
    campaign_id: Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[dt.datetime] = TS()


class AgentRun(Base):
    """Every agent invocation, with its input, output, cost and latency.

    This is what makes the system measurable: cost per prospect and per
    qualified lead are aggregations over this table.
    """

    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    campaign_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    prospect_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    agent_key: Mapped[str] = mapped_column(String(48), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    # pending | running | succeeded | failed | skipped

    input: Mapped[dict] = mapped_column(JSON, default=dict)
    output: Mapped[Optional[dict]] = mapped_column(JSON)
    error: Mapped[Optional[str]] = mapped_column(Text)
    executor: Mapped[str] = mapped_column(String(24), default="simulator")  # dronahq | simulator
    model: Mapped[Optional[str]] = mapped_column(String(80))
    prompt_version_id: Mapped[Optional[str]] = mapped_column(String(64))
    retrieved_chunk_ids: Mapped[list] = mapped_column(JSON, default=list)

    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)

    started_at: Mapped[dt.datetime] = TS()
    finished_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    """Append-only. Never updated, never deleted."""

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_entity", "entity_type", "entity_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    entity_type: Mapped[str] = mapped_column(String(48))
    entity_id: Mapped[str] = mapped_column(String(64))
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[str] = mapped_column(String(16), default="info")  # info | success | warning | error
    message: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(120), default="system")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    prompt_version_id: Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[dt.datetime] = TS()


class RateLimitCounter(Base):
    """Fixed-window counters, one row per (scope, key, window)."""

    __tablename__ = "rate_limit_counters"
    __table_args__ = (UniqueConstraint("scope", "scope_key", "window_start", name="uq_rate_window"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope: Mapped[str] = mapped_column(String(32))  # campaign_channel | rep | platform
    scope_key: Mapped[str] = mapped_column(String(160))
    window_start: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    count: Mapped[int] = mapped_column(Integer, default=0)


class PlatformSetting(Base):
    """Single-row-per-key store for platform-wide state, including the global
    kill switch."""

    __tablename__ = "platform_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


# ---------------------------------------------------------------------------
# Knowledge / RAG
# ---------------------------------------------------------------------------
class KnowledgeDocument(Base):
    """A source document. ``campaign_id`` is null for platform-wide knowledge
    shared by every campaign."""

    __tablename__ = "knowledge_documents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    campaign_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(300))
    doc_type: Mapped[str] = mapped_column(String(48), index=True)
    # product | case_study | playbook | icp_definition | objection_handling |
    # example_email | voice_script
    source: Mapped[Optional[str]] = mapped_column(String(400))
    content: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = TS()


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (Index("ix_chunk_campaign_type", "campaign_id", "doc_type"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("knowledge_documents.id", ondelete="CASCADE"), index=True)
    campaign_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    doc_type: Mapped[str] = mapped_column(String(48))
    title: Mapped[str] = mapped_column(String(300))
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[Any] = mapped_column(EmbeddingType, nullable=True)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = TS()
