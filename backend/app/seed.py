"""Demo seed data for the Atlas SDR platform.

This module builds the account of a real (fictional) sales team at Corvus Labs,
who sell an AI engineering platform called Corvus Flightdeck. It exists so the
product can be demonstrated end to end without a single manual click:

* Four concurrent campaigns with genuinely different ICPs, prompts, policies
  and channel mixes, in three different lifecycle states. Pausing one changes
  nothing about the others, which is the whole point of campaign isolation.
* One human deliberately present in two campaigns under the same
  ``identity_key`` so the duplicate-outreach guardrail fires visibly.
* A knowledge base of real prose, embedded through the normal ingest path, so
  retrieval returns something worth reading rather than lorem.

Run it with ``python -m app.seed``. It is idempotent: a second run against a
populated database does nothing unless ``reset=True`` is passed.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import random
from typing import Any, Optional

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import audit
from .agents.schemas import CAMPAIGN_SYSTEM_PROMPT, DEFAULT_PROMPTS, default_prompt
from .db import init_models, new_id, session_scope
from .models import (
    AgentConfig,
    AgentRun,
    Approval,
    AuditLog,
    Campaign,
    CampaignChannel,
    CampaignRep,
    Company,
    ConversationThread,
    KnowledgeChunk,
    KnowledgeDocument,
    Message,
    PlatformSetting,
    PromptVersion,
    Prospect,
    RateLimitCounter,
    Signal,
    SuppressionEntry,
    User,
    utcnow,
)
from .orchestrator.guardrails import set_kill_switch
from .rag.ingest import ingest_document

# Fixed "now" for the whole seed run, so every relative timestamp in one
# database is internally consistent: a reply is always after its outbound
# message, a pause is always before the present moment.
NOW = utcnow()

RNG = random.Random(20260920)


def ago(days: float = 0, hours: float = 0, minutes: float = 0) -> dt.datetime:
    return NOW - dt.timedelta(days=days, hours=hours, minutes=minutes)


def ahead(days: float = 0, hours: float = 0, minutes: float = 0) -> dt.datetime:
    return NOW + dt.timedelta(days=days, hours=hours, minutes=minutes)


# ---------------------------------------------------------------------------
# Identifiers. Stable and readable so the audit log, the approvals queue and
# the frontend fixtures all line up without a lookup.
# ---------------------------------------------------------------------------
CMP_A = "cmp_us_saas_cto"
CMP_B = "cmp_india_bfsi_cio"
CMP_C = "cmp_voice_ai_founders"
CMP_D = "cmp_enterprise_expansion"

USR_MANAGER = "usr_sm"
USR_RAO = "usr_mr"
USR_IYER = "usr_ai"
USR_FERNANDES = "usr_df"
USR_SHARMA = "usr_ks"


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
USERS: list[dict] = [
    {
        "id": USR_MANAGER,
        "name": "S. Menon",
        "initials": "SM",
        "email": "shalini.menon@corvuslabs.ai",
        "role": "manager",
        "active": True,
        "daily_activity_limit": 200,
        "working_hours": {
            "timezone": "Asia/Kolkata",
            "offset_hours": 5.5,
            "start": 9,
            "end": 20,
            "days": [0, 1, 2, 3, 4],
        },
        "channels_available": ["email", "linkedin", "voice", "sms"],
    },
    {
        "id": USR_RAO,
        "name": "M. Rao",
        "initials": "MR",
        "email": "meera.rao@corvuslabs.ai",
        "role": "rep",
        "active": True,
        "daily_activity_limit": 120,
        "working_hours": {
            "timezone": "America/New_York",
            "offset_hours": -4.0,
            "start": 8,
            "end": 18,
            "days": [0, 1, 2, 3, 4],
        },
        "channels_available": ["email", "linkedin", "voice"],
    },
    {
        "id": USR_IYER,
        "name": "A. Iyer",
        "initials": "AI",
        "email": "aditya.iyer@corvuslabs.ai",
        "role": "rep",
        "active": True,
        # Deliberately narrow: BFSI buyers in India read email between the
        # morning stand-up and lunch, and almost never after 7pm.
        "daily_activity_limit": 45,
        "working_hours": {
            "timezone": "Asia/Kolkata",
            "offset_hours": 5.5,
            "start": 10,
            "end": 19,
            "days": [0, 1, 2, 3, 4],
        },
        "channels_available": ["email", "linkedin"],
    },
    {
        "id": USR_FERNANDES,
        "name": "D. Fernandes",
        "initials": "DF",
        "email": "diya.fernandes@corvuslabs.ai",
        "role": "rep",
        "active": True,
        "daily_activity_limit": 90,
        "working_hours": {
            "timezone": "Europe/Lisbon",
            "offset_hours": 1.0,
            "start": 9,
            "end": 18,
            "days": [0, 1, 2, 3, 4],
        },
        "channels_available": ["email", "linkedin", "sms"],
    },
    {
        "id": USR_SHARMA,
        "name": "K. Sharma",
        "initials": "KS",
        "email": "karan.sharma@corvuslabs.ai",
        # Offboarded last quarter. Left active=False on purpose: his identity
        # must never be picked as a sending identity again, and the platform
        # has to show that rather than silently keep sending as him.
        "role": "rep",
        "active": False,
        "daily_activity_limit": 0,
        "working_hours": {
            "timezone": "Asia/Kolkata",
            "offset_hours": 5.5,
            "start": 10,
            "end": 18,
            "days": [0, 1, 2, 3, 4],
        },
        "channels_available": ["email"],
    },
]


# ---------------------------------------------------------------------------
# The product every campaign is selling. Identical across campaigns on
# purpose: only the ICP changes, which is what makes the four campaigns a fair
# comparison of targeting rather than four different companies.
# ---------------------------------------------------------------------------
PRODUCT_CONTEXT: dict = {
    "vendor": "Corvus Labs",
    "name": "Corvus Flightdeck",
    "category": "AI engineering platform",
    "one_liner": "Flightdeck reviews every pull request with a model that has read the whole repository, attributes delivery metrics down to the individual PR, and keeps AI-written code inside policy.",
    "description": (
        "Corvus Flightdeck installs on top of an engineering organisation's existing "
        "GitHub or GitLab, CI and issue tracker. It does three jobs that today sit with "
        "senior engineers: it reviews every pull request with a model that has indexed the "
        "whole repository and its review history, it attributes DORA metrics to individual "
        "pull requests so leaders can see exactly where delivery slows down, and it applies "
        "policy to AI-generated code so a team can adopt coding agents without losing the "
        "audit trail. It runs in the customer's cloud account or fully self-hosted, and it "
        "never trains on customer code."
    ),
    "modules": [
        {"name": "Flightdeck Review", "summary": "Repository-aware pull request review that catches cross-file regressions, not style nits."},
        {"name": "Flightdeck Insights", "summary": "DORA and cycle-time analytics attributed per pull request, per service and per team."},
        {"name": "Flightdeck Agents", "summary": "Governed coding agents for migrations, test backfill and dependency upgrades."},
        {"name": "Flightdeck Guard", "summary": "Policy, provenance and audit for every line of AI-generated code that reaches main."},
    ],
    "use_cases": [
        "Cut pull request review latency without adding reviewers",
        "Bring change failure rate down after a period of fast hiring",
        "Prove where engineering time actually goes, per service and per team",
        "Adopt coding agents with provenance and policy instead of banning them",
        "Retire overlapping code quality, analytics and SAST tooling into one line item",
        "Get new engineers to their first merged pull request in days rather than weeks",
    ],
    "pricing": [
        {"tier": "Team", "price": "$39 per developer per month", "notes": "Review and Insights. Up to 60 developers, cloud only, self-serve."},
        {"tier": "Scale", "price": "$69 per developer per month", "notes": "Adds Agents, SSO/SCIM, private VPC deployment, 99.9% SLA."},
        {"tier": "Enterprise", "price": "From $148,000 per year", "notes": "Adds Guard, self-hosted inference, data residency, named architect, custom DPA."},
    ],
    "differentiators": [
        "Runs entirely inside the customer's VPC, or fully air-gapped with self-hosted inference, so source code never leaves their perimeter.",
        "Contractual no-training guarantee on customer code and customer prompts, backed by the DPA rather than a marketing page.",
        "Repository-wide index rather than diff-only review, which is why it finds cross-file regressions competitors miss.",
        "Delivery metrics attributed per pull request, so an engineering leader can defend a headcount case with evidence.",
        "In-region data residency, including ap-south-1 for India, with customer-managed keys.",
    ],
    "proof_points": [
        "Hollowell Commerce cut median pull request review latency from 19 hours to 7 hours in one quarter.",
        "Deccan Federal Bank cleared a 14,000-file COBOL-to-Java migration backlog in five months with two engineers supervising Flightdeck Agents.",
        "Median customer retires 2.4 overlapping tools in the first year.",
    ],
    "integrations": ["GitHub Enterprise", "GitLab Ultimate", "Bitbucket Data Center", "Jira", "Linear", "Jenkins", "CircleCI", "Datadog", "Okta", "Azure AD"],
    "security": ["SOC 2 Type II", "ISO 27001", "ISO 27701", "GDPR", "DPDP Act 2023 aligned", "customer-managed encryption keys"],
}


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------
CAMPAIGNS: list[dict] = [
    {
        "id": CMP_A,
        "name": "US SaaS · CTO Outreach",
        "label": "Campaign A",
        "status": "live",
        "owner_id": USR_RAO,
        "reps": [(USR_RAO, True), (USR_MANAGER, False)],
        "icp_summary": "SaaS, Series B–D, CTO / VP Eng, 50–500 FTE",
        "description": (
            "Our core motion. US-headquartered B2B SaaS companies that have raised a "
            "Series B to Series D, hired fast, and are now paying for that speed in "
            "review latency and change failure rate."
        ),
        "objective": (
            "Book 25 qualified discovery calls a quarter with CTOs and VPs of Engineering at "
            "US B2B SaaS companies between Series B and Series D, by leading with a specific, "
            "dated signal about their engineering org rather than a product pitch."
        ),
        "created_at": dt.datetime(2026, 8, 12, 9, 0, tzinfo=dt.timezone.utc),
        "updated_at": ago(hours=2),
        "icp": {
            "industry": ["B2B SaaS", "Vertical SaaS", "Developer tools", "Fintech infrastructure"],
            "funding_stage": ["Series B", "Series C", "Series D"],
            "headcount": {"min": 50, "max": 500},
            "engineering_headcount": {"min": 25},
            "titles": ["CTO", "VP Engineering", "VP Platform Engineering", "SVP Engineering", "Head of Engineering"],
            "seniority": ["C-level", "SVP", "VP"],
            "geography": ["United States"],
            "tech_stack_signals": ["GitHub Enterprise", "GitLab", "Kubernetes", "Terraform", "Datadog", "LaunchDarkly"],
            "buying_triggers": [
                "Engineering headcount grew more than 30% in the last two quarters",
                "Published an engineering blog post about review bottlenecks, incident load or AI tooling",
                "Hired a first Head of Platform or Developer Experience",
                "Public incident or status-page outage cluster in the last 90 days",
            ],
            "exclusions": [
                "Companies below 25 engineers",
                "Pre-Series B and bootstrapped",
                "Agencies, consultancies and staffing firms",
                "Direct competitors in AI code review",
            ],
            "signal_types": ["hiring", "funding", "product_launch", "leadership_change", "engineering_blog", "incident"],
            "freshness_days": 90,
        },
        "policies": {
            "daily_send_limit": 60,
            "qualification_threshold": 75,
            "borderline_threshold": 58,
            "max_follow_ups": 3,
            "follow_up_gap_hours": 72,
            "require_approval_above_seniority": ["CEO", "Founder", "President", "SVP"],
            "require_approval_below_confidence": 0.62,
            "channel_priority": ["email", "linkedin", "voice"],
            "working_hours_only": True,
        },
        "channels": [
            ("email", "active", 40),
            ("linkedin", "active", 25),
            ("voice", "active", 8),
        ],
    },
    {
        "id": CMP_B,
        "name": "India BFSI · CIO Outreach",
        "label": "Campaign B",
        "status": "paused",
        "owner_id": USR_IYER,
        "reps": [(USR_IYER, True)],
        "paused_at": ago(hours=3),
        "paused_by_id": USR_IYER,
        "icp_summary": "BFSI, CIO / Head of IT",
        "description": (
            "Regulated buyers: banks, insurers, NBFCs and broking houses in India with "
            "in-house engineering of real size. Long cycles, procurement-heavy, and won by "
            "answering data residency and audit questions before they are asked."
        ),
        "objective": (
            "Open credible conversations with CIOs and Heads of IT at Indian banks, insurers and "
            "NBFCs above 1,000 employees, leading with in-country data residency and auditable "
            "provenance for AI-generated code, and escalating any procurement or security "
            "question to a human on the first touch."
        ),
        "created_at": dt.datetime(2026, 8, 20, 9, 0, tzinfo=dt.timezone.utc),
        "updated_at": ago(hours=3),
        "icp": {
            "industry": ["Banking", "Insurance", "NBFC", "Capital markets", "Payments"],
            "headcount": {"min": 1000},
            "in_house_engineering": {"min": 120},
            "titles": ["CIO", "Chief Information Officer", "Head of IT", "Chief Technology Officer", "Head of Digital Technology", "VP Technology"],
            "seniority": ["C-level", "Head", "VP"],
            "geography": ["India"],
            "regulatory_context": ["RBI IT Governance Directions 2023", "IRDAI Information and Cyber Security Guidelines", "SEBI CSCRF", "DPDP Act 2023"],
            "tech_stack_signals": ["Java", "Mainframe / COBOL", "Temenos", "Finacle", "On-premise GitLab", "Private cloud"],
            "buying_triggers": [
                "Announced a core banking or policy administration modernisation programme",
                "Opened or expanded a captive technology centre",
                "Published a regulatory observation on IT change management",
                "Appointed a new CIO or Chief Digital Officer in the last two quarters",
            ],
            "exclusions": [
                "Institutions that have fully outsourced application development",
                "Under 1,000 total employees",
                "Cooperative banks without an in-house technology function",
            ],
            "signal_types": ["regulatory", "hiring", "leadership_change", "expansion", "partnership", "earnings_call"],
            "freshness_days": 120,
        },
        "policies": {
            # Deliberately conservative. A mistimed or over-familiar email to a
            # bank CIO does not just fail, it closes the account for a year.
            "daily_send_limit": 18,
            "qualification_threshold": 70,
            "borderline_threshold": 55,
            "max_follow_ups": 2,
            "follow_up_gap_hours": 120,
            "require_approval_above_seniority": ["CXO", "CIO", "CEO", "Managing Director", "Founder"],
            "require_approval_below_confidence": 0.75,
            "channel_priority": ["email", "linkedin"],
            "working_hours_only": True,
        },
        "channels": [
            ("email", "active", 12),
            ("linkedin", "active", 6),
        ],
    },
    {
        "id": CMP_C,
        "name": "Voice AI Founders",
        "label": "Campaign C",
        "status": "live",
        "owner_id": USR_RAO,
        "reps": [(USR_RAO, True), (USR_FERNANDES, False)],
        "icp_summary": "AI founders, Seed–Series B",
        "description": (
            "Small, fast, technical founding teams building voice and speech products in the "
            "US and EU. They already run coding agents; what they lack is the review and "
            "provenance layer to keep that safe as the team triples."
        ),
        "objective": (
            "Get 15 founder conversations a month with technical founders of Seed to Series B "
            "voice and speech AI companies in the US and EU, on the argument that governed "
            "coding agents let a 12-engineer team ship like a 30-engineer one."
        ),
        "created_at": dt.datetime(2026, 9, 1, 9, 0, tzinfo=dt.timezone.utc),
        "updated_at": ago(minutes=45),
        "icp": {
            "industry": ["Voice AI", "Speech recognition", "Conversational AI", "Audio infrastructure"],
            "funding_stage": ["Pre-seed", "Seed", "Series A", "Series B"],
            "headcount": {"min": 5, "max": 120},
            "titles": ["Founder", "Co-founder", "CEO", "CTO", "Chief Scientist", "Head of Engineering"],
            "seniority": ["Founder", "C-level"],
            "geography": ["United States", "United Kingdom", "Germany", "Sweden", "Ireland", "France", "Netherlands"],
            "tech_stack_signals": ["Python", "Rust", "GPU inference", "Modal", "Temporal", "GitHub"],
            "buying_triggers": [
                "Raised a priced round in the last 120 days",
                "Shipped a public API or developer preview",
                "Founder posted publicly about AI-assisted development or review load",
                "Hiring their first platform or infrastructure engineer",
            ],
            "exclusions": [
                "Post-Series B or above 120 employees",
                "Non-technical primary contact",
                "Agencies and implementation partners",
            ],
            "signal_types": ["funding", "product_launch", "hiring", "founder_post", "conference_talk", "open_source"],
            "freshness_days": 60,
        },
        "policies": {
            # Founders reply fast or never. Shorter gaps, more attempts, and a
            # lower confidence bar because the downside of a mediocre email to
            # a 14-person startup is a shrug, not a lost account.
            "daily_send_limit": 45,
            "qualification_threshold": 68,
            "borderline_threshold": 52,
            "max_follow_ups": 4,
            "follow_up_gap_hours": 40,
            "require_approval_above_seniority": ["Founder", "CEO", "Co-founder"],
            "require_approval_below_confidence": 0.55,
            "channel_priority": ["email", "voice", "sms"],
            "working_hours_only": False,
        },
        "channels": [
            ("email", "active", 30),
            ("voice", "active", 10),
            # Paused by the manager after an SMS landed at 06:40 local time in
            # Berlin. The campaign kept running on email and voice.
            ("sms", "paused", 12),
        ],
    },
    {
        "id": CMP_D,
        "name": "Enterprise Expansion",
        "label": "Campaign D",
        "status": "draft",
        "owner_id": USR_MANAGER,
        "reps": [(USR_MANAGER, False)],
        "icp_summary": "Existing customers",
        "description": (
            "Expansion into existing Enterprise accounts: teams already on Review and Insights "
            "who have not yet bought Guard. Still a draft — no channels configured, so nothing "
            "can be sent from it."
        ),
        "objective": (
            "Expand existing Enterprise accounts from Review and Insights into Guard and Agents "
            "by starting from the customer's own adoption data, not a cold pitch."
        ),
        "created_at": dt.datetime(2026, 9, 14, 9, 0, tzinfo=dt.timezone.utc),
        "updated_at": ago(days=1, hours=1),
        "icp": {
            "relationship": "existing_customer",
            "industry": ["Any"],
            "contract_tier": ["Enterprise"],
            "titles": ["CTO", "VP Engineering", "Head of Platform", "Director of Engineering"],
            "geography": ["United States", "India", "European Union"],
            "buying_triggers": [
                "Review adoption above 70% of active repositories",
                "Coding agent usage detected without Guard policy coverage",
                "Renewal window opens within 120 days",
            ],
            "exclusions": ["Accounts in an open escalation", "Accounts below 90 days tenure"],
            "signal_types": ["product_usage", "renewal_window", "support_escalation"],
            "freshness_days": 30,
        },
        "policies": {
            "daily_send_limit": 20,
            "qualification_threshold": 70,
            "borderline_threshold": 55,
            "max_follow_ups": 2,
            "follow_up_gap_hours": 96,
            "require_approval_above_seniority": ["CEO", "Founder", "President"],
            "require_approval_below_confidence": 0.7,
            "channel_priority": ["email"],
            "working_hours_only": True,
        },
        "channels": [],
    },
]


# ---------------------------------------------------------------------------
# Agent configuration. The two mechanical agents run on the cheap model; the
# three that write or decide run on Sonnet. That split is most of the cost
# story in the metrics panel.
# ---------------------------------------------------------------------------
CHEAP_MODEL = "claude-haiku-4-5"
SMART_MODEL = "claude-sonnet-4-6"

AGENT_CONFIGS: dict[str, list[dict]] = {
    CMP_A: [
        {
            "agent_key": "prospect_generation",
            "model": SMART_MODEL,
            "tools": ["web_search", "apollo_enrichment", "company_site", "knowledge_base"],
            "thresholds": {"batch_size": 25, "min_match_confidence": 0.6, "max_per_company": 2},
            "escalation_rules": {"on_empty_batch": "notify_owner", "on_exclusion_hit": "skip_silently"},
        },
        {
            "agent_key": "research_enrichment",
            "model": CHEAP_MODEL,
            "tools": ["web_search", "apollo_enrichment", "company_site"],
            "thresholds": {"min_signals": 1, "signal_freshness_days": 90, "min_confidence": 0.55},
            "escalation_rules": {"on_no_signals": "mark_missing_fields", "on_confidence_below": 0.4},
        },
        {
            "agent_key": "icp_fit",
            "model": CHEAP_MODEL,
            "tools": ["knowledge_base"],
            "thresholds": {"qualified": 75, "borderline": 58, "auto_reject_below": 40},
            "escalation_rules": {"borderline_verdict": "human_approval", "reason": "Borderline ICP fit"},
        },
        {
            "agent_key": "outreach_strategy",
            "model": SMART_MODEL,
            "tools": ["knowledge_base", "scheduler"],
            "thresholds": {"max_follow_ups": 3, "channel_switch_after_silent": 2, "wait_hours_default": 72},
            "escalation_rules": {"seniority_above": ["SVP", "President", "CEO", "Founder"], "on_pricing_question": "escalate_human"},
        },
        {
            "agent_key": "personalisation",
            "model": SMART_MODEL,
            "tools": ["knowledge_base", "company_site"],
            "thresholds": {"min_confidence": 0.62, "word_count": {"email": [60, 110], "linkedin": [40, 90]}, "min_evidence_items": 1},
            "escalation_rules": {"below_confidence": "human_approval", "uncited_claim": "block_and_redraft"},
        },
        {
            "agent_key": "conversation",
            "model": SMART_MODEL,
            "tools": ["knowledge_base", "scheduler"],
            "thresholds": {"min_confidence": 0.7, "auto_reply_max_words": 90},
            "escalation_rules": {"intents": ["pricing", "security_review", "legal", "complaint"], "on_opt_out": "close_and_suppress"},
        },
    ],
    CMP_B: [
        {
            "agent_key": "prospect_generation",
            "model": SMART_MODEL,
            "tools": ["web_search", "company_site", "knowledge_base"],
            "thresholds": {"batch_size": 10, "min_match_confidence": 0.75, "max_per_company": 1},
            "escalation_rules": {"on_empty_batch": "notify_owner", "on_regulated_entity": "verify_before_add"},
        },
        {
            "agent_key": "research_enrichment",
            "model": CHEAP_MODEL,
            "tools": ["web_search", "company_site", "knowledge_base"],
            "thresholds": {"min_signals": 2, "signal_freshness_days": 120, "min_confidence": 0.7},
            "escalation_rules": {"on_no_signals": "hold_for_human", "on_confidence_below": 0.6},
        },
        {
            "agent_key": "icp_fit",
            "model": CHEAP_MODEL,
            "tools": ["knowledge_base"],
            "thresholds": {"qualified": 70, "borderline": 55, "auto_reject_below": 38},
            "escalation_rules": {"borderline_verdict": "human_approval", "outsourced_it_detected": "auto_reject"},
        },
        {
            "agent_key": "outreach_strategy",
            "model": SMART_MODEL,
            "tools": ["knowledge_base", "scheduler"],
            "thresholds": {"max_follow_ups": 2, "channel_switch_after_silent": 2, "wait_hours_default": 120},
            "escalation_rules": {"seniority_above": ["CXO", "CIO", "Managing Director"], "on_any_compliance_question": "escalate_human"},
        },
        {
            "agent_key": "personalisation",
            "model": SMART_MODEL,
            "tools": ["knowledge_base"],
            "thresholds": {"min_confidence": 0.75, "word_count": {"email": [70, 120], "linkedin": [40, 80]}, "min_evidence_items": 2},
            "escalation_rules": {"below_confidence": "human_approval", "mentions_regulation": "human_approval"},
        },
        {
            "agent_key": "conversation",
            "model": SMART_MODEL,
            "tools": ["knowledge_base", "scheduler"],
            "thresholds": {"min_confidence": 0.82, "auto_reply_max_words": 70},
            "escalation_rules": {"intents": ["pricing", "security_review", "legal", "procurement", "rfp"], "on_opt_out": "close_and_suppress"},
        },
    ],
    CMP_C: [
        {
            "agent_key": "prospect_generation",
            "model": SMART_MODEL,
            "tools": ["web_search", "apollo_enrichment", "company_site"],
            "thresholds": {"batch_size": 30, "min_match_confidence": 0.55, "max_per_company": 2},
            "escalation_rules": {"on_empty_batch": "widen_geography", "on_exclusion_hit": "skip_silently"},
        },
        {
            "agent_key": "research_enrichment",
            "model": CHEAP_MODEL,
            "tools": ["web_search", "company_site", "apollo_enrichment"],
            "thresholds": {"min_signals": 1, "signal_freshness_days": 60, "min_confidence": 0.5},
            "escalation_rules": {"on_no_signals": "mark_missing_fields"},
        },
        {
            "agent_key": "icp_fit",
            "model": CHEAP_MODEL,
            "tools": ["knowledge_base"],
            "thresholds": {"qualified": 68, "borderline": 52, "auto_reject_below": 35},
            "escalation_rules": {"borderline_verdict": "human_approval"},
        },
        {
            "agent_key": "outreach_strategy",
            "model": SMART_MODEL,
            "tools": ["knowledge_base", "scheduler"],
            "thresholds": {"max_follow_ups": 4, "channel_switch_after_silent": 1, "wait_hours_default": 40},
            "escalation_rules": {"seniority_above": ["Founder", "CEO", "Co-founder"], "on_pricing_question": "escalate_human"},
        },
        {
            "agent_key": "personalisation",
            "model": SMART_MODEL,
            "tools": ["knowledge_base", "company_site"],
            "thresholds": {"min_confidence": 0.55, "word_count": {"email": [55, 95], "sms": [15, 40]}, "min_evidence_items": 1},
            "escalation_rules": {"below_confidence": "human_approval", "uncited_claim": "block_and_redraft"},
        },
        {
            "agent_key": "conversation",
            # Paused after it answered a pricing question on its own last
            # Thursday. The campaign keeps prospecting and sending; only replies
            # now wait for a human.
            "state": "paused",
            "model": SMART_MODEL,
            "tools": ["knowledge_base", "scheduler"],
            "thresholds": {"min_confidence": 0.7, "auto_reply_max_words": 80},
            "escalation_rules": {"intents": ["pricing", "security_review", "legal"], "on_opt_out": "close_and_suppress"},
        },
    ],
    CMP_D: [
        {
            "agent_key": "prospect_generation",
            "model": SMART_MODEL,
            "tools": ["knowledge_base", "product_usage"],
            "thresholds": {"batch_size": 15, "min_match_confidence": 0.8},
            "escalation_rules": {"on_open_escalation": "skip_account"},
        },
        {
            "agent_key": "research_enrichment",
            "model": CHEAP_MODEL,
            "tools": ["product_usage", "knowledge_base"],
            "thresholds": {"min_signals": 1, "signal_freshness_days": 30, "min_confidence": 0.7},
            "escalation_rules": {"on_no_signals": "hold_for_human"},
        },
        {
            "agent_key": "icp_fit",
            "model": CHEAP_MODEL,
            "tools": ["knowledge_base", "product_usage"],
            "thresholds": {"qualified": 70, "borderline": 55, "auto_reject_below": 40},
            "escalation_rules": {"borderline_verdict": "human_approval"},
        },
        {
            "agent_key": "outreach_strategy",
            "model": SMART_MODEL,
            "tools": ["knowledge_base", "scheduler"],
            "thresholds": {"max_follow_ups": 2, "wait_hours_default": 96},
            "escalation_rules": {"on_renewal_within_days": 45},
        },
        {
            "agent_key": "personalisation",
            "model": SMART_MODEL,
            "tools": ["knowledge_base", "product_usage"],
            "thresholds": {"min_confidence": 0.7, "word_count": {"email": [70, 120]}, "min_evidence_items": 1},
            "escalation_rules": {"below_confidence": "human_approval"},
        },
        {
            "agent_key": "conversation",
            "model": SMART_MODEL,
            "tools": ["knowledge_base", "scheduler"],
            "thresholds": {"min_confidence": 0.75, "auto_reply_max_words": 90},
            "escalation_rules": {"intents": ["pricing", "renewal", "legal"], "on_opt_out": "close_and_suppress"},
        },
    ],
}


# ---------------------------------------------------------------------------
# Prompt history for the personalisation agent on Campaign A.
#
# Built by editing the shipped default rather than retyping it, so the version
# history on the prompt-management page shows a real, reviewable diff: v2
# changed the call to action, v3 added an evidence-freshness rule.
# ---------------------------------------------------------------------------
_PERSONALISATION_V1 = default_prompt("personalisation")

_CTA_OLD = "- One clear, low-friction call to action."
_CTA_NEW = (
    "- One clear, low-friction call to action. Ask whether the problem is real for\n"
    "  them, not for thirty minutes of their calendar. A first touch that asks a\n"
    "  question gets answered; a first touch that asks for a meeting gets archived.\n"
    "  Let them offer the call."
)
_PERSONALISATION_V2 = (
    _PERSONALISATION_V1.replace(_CTA_OLD, _CTA_NEW)
    if _CTA_OLD in _PERSONALISATION_V1
    else _PERSONALISATION_V1 + "\n\n" + _CTA_NEW
)

_FRESHNESS_RULE = (
    "- Never reference a funding round, raise or valuation observed more than six\n"
    "  months ago. A stale round reads as a scraped list rather than research, and\n"
    "  it is the single most common reason our first touches get marked as spam.\n"
    "  If the only signal you have is an old round, say less and lead with the\n"
    "  product problem instead."
)
_PERSONALISATION_V3 = _PERSONALISATION_V2.replace(
    _CTA_NEW, _FRESHNESS_RULE + "\n" + _CTA_NEW
) if _CTA_NEW in _PERSONALISATION_V2 else _PERSONALISATION_V2 + "\n\n" + _FRESHNESS_RULE

PERSONALISATION_HISTORY = [
    {
        "version": 1,
        "content": _PERSONALISATION_V1,
        "notes": "Seeded from the platform default when the campaign was created.",
        "is_active": False,
        "created_by_id": USR_MANAGER,
        "created_at": dt.datetime(2026, 8, 12, 9, 12, tzinfo=dt.timezone.utc),
        "activated_at": dt.datetime(2026, 8, 12, 9, 12, tzinfo=dt.timezone.utc),
    },
    {
        "version": 2,
        "content": _PERSONALISATION_V2,
        "notes": "Tightened the CTA, v1 was asking for 30 minutes too early.",
        "is_active": False,
        "created_by_id": USR_RAO,
        "created_at": ago(days=12, hours=4),
        "activated_at": ago(days=12, hours=4),
    },
    {
        "version": 3,
        "content": _PERSONALISATION_V3,
        "notes": "Added an explicit ban on referencing funding rounds older than 6 months.",
        "is_active": True,
        "created_by_id": USR_FERNANDES,
        "created_at": ago(days=4, hours=6),
        "activated_at": ago(days=4, hours=5, minutes=50),
    },
]


# ---------------------------------------------------------------------------
# Prospects
#
# Each entry carries its company, its research block, its dated signals and,
# where it has one, its conversation. Stages are spread on purpose so the
# funnel chart on the campaign detail page has a shape.
# ---------------------------------------------------------------------------
def _fit(industry: int, seniority: int, size: int, geography: int, signal: int) -> dict:
    return {
        "industry": industry,
        "seniority": seniority,
        "company_size": size,
        "geography": geography,
        "signal_strength": signal,
    }


PROSPECTS_A: list[dict] = [
    {
        "id": "psp_a_okafor",
        "company": {
            "id": "co_brightpath",
            "name": "Brightpath Analytics",
            "domain": "brightpath.io",
            "industry": "Product analytics SaaS",
            "headcount": 310,
            "hq_location": "Austin, TX",
            "funding_stage": "Series C",
            "enrichment": {
                "engineers": 138,
                "arr_estimate_usd": "41M",
                "repos": "GitHub Enterprise, 214 active repositories",
                "stack": ["Go", "TypeScript", "Kubernetes", "Snowflake"],
                "last_round": {"amount_usd": "58M", "date": "2026-06-18", "lead": "Bracken Ridge Ventures"},
            },
        },
        "full_name": "Daniel Okafor",
        "designation": "Chief Technology Officer",
        "seniority": "C-level",
        "email": "daniel.okafor@brightpath.io",
        "phone": "+1-512-555-0142",
        "linkedin_url": "https://www.linkedin.com/in/danielokafor-cto",
        "persona_match": "Scaling CTO, post-Series C, review latency pain",
        "source": "Engineering blog + Apollo",
        "stage": "meeting",
        "status": "meeting_booked",
        "fit_score": 91.0,
        "fit_verdict": "qualified",
        "fit_criteria": _fit(95, 100, 88, 100, 82),
        "fit_rationale": (
            "Textbook fit. Series C product analytics company in Austin with 138 engineers, "
            "inside the 50–500 band and well past the 25-engineer floor. Okafor is the CTO and "
            "the named owner of the developer experience programme they wrote about publicly. "
            "The dated signal is strong and specific: their own engineering blog says review "
            "latency is their top constraint, which is the exact problem Flightdeck Review is "
            "bought for. Nothing in the exclusion list applies."
        ),
        "research": {
            "company": {
                "name": "Brightpath Analytics",
                "what_they_do": "Self-serve product analytics for B2B software teams.",
                "engineering_org": "138 engineers across 11 squads, platform team formed Q1 2026.",
                "delivery_signals": "Public engineering blog states median PR review wait is 21 hours.",
            },
            "person": {
                "name": "Daniel Okafor",
                "role": "CTO since 2023, previously VP Engineering at a logistics SaaS.",
                "public_writing": "Authors the Brightpath engineering blog; spoke at LeadDev Austin 2026.",
                "likely_priorities": "Developer experience, cost of the Snowflake bill, hiring quality bar.",
            },
            "signals": ["engineering_blog:review-latency", "hiring:platform-team", "funding:series-c"],
            "contact_channels": {"email": "verified", "linkedin": "active", "phone": "direct dial, unverified"},
            "inferred_pain_points": [
                "Review latency is the stated bottleneck and it is getting worse with headcount",
                "Platform team is new and has no baseline metric to defend its roadmap",
                "Cross-service regressions are being caught in staging rather than in review",
            ],
            "confidence": 0.89,
            "missing_fields": [],
        },
        "signals": [
            {
                "signal_type": "engineering_blog",
                "summary": "Brightpath's engineering blog put median pull request review wait at 21 hours and named it the team's top delivery constraint for 2026.",
                "source_url": "https://brightpath.io/engineering/the-21-hour-review-queue",
                "days_ago": 23,
                "confidence": 0.94,
            },
            {
                "signal_type": "hiring",
                "summary": "Opened four Developer Experience and Platform Engineer roles in Austin, all reporting into a platform team formed this year.",
                "source_url": "https://brightpath.io/careers?team=platform",
                "days_ago": 31,
                "confidence": 0.86,
            },
        ],
        "follow_up_count": 1,
        "last_contacted_days": 6,
        "next_action_days": 2,
        "current_channel": "email",
        "channels_tried": ["email", "linkedin"],
        "thread": {
            "channel": "email",
            "status": "meeting_booked",
            "sentiment": "positive",
            "messages": [
                {
                    "direction": "outbound",
                    "message_type": "initial_outreach",
                    "subject": "The 21-hour review queue post",
                    "body": (
                        "Daniel — your post on the 21-hour review queue is the clearest write-up of that "
                        "problem I have read this year, particularly the part about senior reviewers "
                        "becoming the bottleneck exactly when you need them on architecture.\n\n"
                        "We built Flightdeck for that shape of problem. It indexes the whole repository "
                        "rather than just the diff, so it catches cross-file regressions before a human "
                        "opens the PR. Hollowell Commerce, roughly your size, went from 19 hours to 7.\n\n"
                        "Is review latency still the number one constraint now the platform team is in "
                        "place, or has it moved?"
                    ),
                    "status": "replied",
                    "days_ago": 14,
                    "confidence": 0.88,
                    "evidence_signal": 0,
                },
                {
                    "direction": "inbound",
                    "message_type": "reply",
                    "subject": "Re: The 21-hour review queue post",
                    "body": (
                        "Still number one, and honestly worse than when I wrote that — we added 20 "
                        "engineers since. The platform team has been firefighting CI flakiness instead.\n\n"
                        "I am sceptical of anything that reviews PRs, we tried two of these and both "
                        "produced style noise our seniors learned to ignore. If yours genuinely catches "
                        "cross-file stuff I will look. Send me something I can run against one repo "
                        "without a procurement conversation."
                    ),
                    "status": "delivered",
                    "days_ago": 12,
                },
                {
                    "direction": "outbound",
                    "message_type": "follow_up",
                    "subject": "Re: The 21-hour review queue post",
                    "body": (
                        "That scepticism is fair and it is the right test. Style nits are what you get "
                        "from diff-only review; the whole reason we index the repository is to surface "
                        "the caller you forgot to update three services away.\n\n"
                        "Single-repo trial needs no procurement: read-only GitHub app, your VPC, no "
                        "training on your code. Thursday or Friday next week for 25 minutes with our "
                        "solutions architect and we can point it at the noisiest repo you have."
                    ),
                    "status": "replied",
                    "days_ago": 10,
                    "confidence": 0.84,
                    "evidence_signal": 0,
                },
                {
                    "direction": "inbound",
                    "message_type": "reply",
                    "subject": "Re: The 21-hour review queue post",
                    "body": (
                        "Friday works. 10am Central. I will bring Priyanka who owns platform.\n\n"
                        "Point it at billing-core, it is our worst offender and the one where a missed "
                        "caller costs us actual money."
                    ),
                    "status": "delivered",
                    "days_ago": 6,
                },
            ],
        },
    },
    {
        "id": "psp_a_feld",
        "company": {
            "id": "co_northwind",
            "name": "Northwind Ledger",
            "domain": "northwindledger.com",
            "industry": "Accounting and close automation SaaS",
            "headcount": 480,
            "hq_location": "Boston, MA",
            "funding_stage": "Series D",
            "enrichment": {
                "engineers": 196,
                "arr_estimate_usd": "88M",
                "repos": "GitHub Enterprise, monorepo plus 40 services",
                "stack": ["Java", "Kotlin", "React", "AWS", "Terraform"],
                "compliance": ["SOC 2 Type II", "PCI DSS"],
            },
        },
        "full_name": "Marcus Feld",
        "designation": "Chief Technology Officer",
        "seniority": "C-level",
        "email": "marcus.feld@northwindledger.com",
        "phone": None,
        "linkedin_url": "https://www.linkedin.com/in/marcusfeld",
        "persona_match": "Series D CTO under change-failure pressure",
        "source": "Status page monitoring + web research",
        "stage": "engaged",
        "status": "replied",
        "fit_score": 87.0,
        "fit_verdict": "qualified",
        "fit_criteria": _fit(92, 100, 80, 100, 79),
        "fit_rationale": (
            "Strong fit with one caveat. Series D and 480 employees puts them at the top of the "
            "50–500 band, so the buying process will look more enterprise than the rest of this "
            "campaign. Against that, 196 engineers and a PCI-regulated close product make change "
            "failure rate a board-level number for Feld, and their status page shows a real "
            "cluster of incidents traced to deploys. Qualified, but route the security "
            "questionnaire early."
        ),
        "research": {
            "company": {
                "name": "Northwind Ledger",
                "what_they_do": "Financial close and reconciliation automation for mid-market finance teams.",
                "engineering_org": "196 engineers, monorepo plus 40 services, two-week release train.",
                "delivery_signals": "Five customer-visible incidents in 60 days, four attributed to deploys.",
            },
            "person": {
                "name": "Marcus Feld",
                "role": "CTO, joined from a payments processor in 2024.",
                "public_writing": "Quoted in a trade press piece on AI code review governance.",
                "likely_priorities": "Change failure rate, PCI audit readiness, release train throughput.",
            },
            "signals": ["incident:deploy-cluster", "leadership_change:vp-platform", "press:ai-governance"],
            "contact_channels": {"email": "verified", "linkedin": "active", "phone": "not found"},
            "inferred_pain_points": [
                "Change failure rate is visible to customers and to the audit committee",
                "No per-PR attribution, so post-incident reviews argue about causes",
                "AI-assisted code is already in the repo without a provenance trail",
            ],
            "confidence": 0.81,
            "missing_fields": ["direct_phone"],
        },
        "signals": [
            {
                "signal_type": "incident",
                "summary": "Northwind's public status page logged five customer-visible incidents in 60 days, four of them opening within an hour of a scheduled deploy.",
                "source_url": "https://status.northwindledger.com/history",
                "days_ago": 11,
                "confidence": 0.88,
            },
            {
                "signal_type": "leadership_change",
                "summary": "Appointed a first VP of Platform Engineering in August with an explicit remit for release reliability.",
                "source_url": "https://www.northwindledger.com/newsroom/vp-platform-engineering",
                "days_ago": 38,
                "confidence": 0.9,
            },
            {
                "signal_type": "press",
                "summary": "Feld told FinTech Weekly that Northwind is writing an internal policy for AI-generated code before it expands agent usage.",
                "source_url": "https://fintechweekly.example.com/northwind-ai-code-policy",
                "days_ago": 19,
                "confidence": 0.76,
            },
        ],
        "follow_up_count": 1,
        "last_contacted_days": 3,
        "next_action_days": 1,
        "current_channel": "email",
        "channels_tried": ["email"],
        "thread": {
            "channel": "email",
            "status": "active",
            "sentiment": "positive",
            "messages": [
                {
                    "direction": "outbound",
                    "message_type": "initial_outreach",
                    "subject": "Four of five incidents inside an hour of a deploy",
                    "body": (
                        "Marcus — your status page history for the last 60 days shows five "
                        "customer-visible incidents, and four opened within an hour of a scheduled "
                        "deploy. With a PCI-regulated close product that pattern tends to become an "
                        "audit committee question before it becomes an engineering one.\n\n"
                        "Flightdeck attributes change failure rate to the individual pull request, so "
                        "a post-incident review starts from evidence instead of argument, and Guard "
                        "keeps AI-written code inside the policy you told FinTech Weekly you were "
                        "drafting.\n\nWorth twenty minutes with whoever owns the release train?"
                    ),
                    "status": "replied",
                    "days_ago": 8,
                    "confidence": 0.83,
                    "evidence_signal": 0,
                },
                {
                    "direction": "inbound",
                    "message_type": "reply",
                    "subject": "Re: Four of five incidents inside an hour of a deploy",
                    "body": (
                        "You have read our status page more carefully than some of my own team, which "
                        "is either impressive or alarming.\n\n"
                        "The AI policy is real and it is the thing I actually care about — we have "
                        "engineers using agents and no way to prove what came from where. Before any "
                        "call I need to know: can this run entirely in our AWS account, and will you "
                        "sign something saying our code is never used for training? Those two answers "
                        "decide whether there is a conversation."
                    ),
                    "status": "delivered",
                    "days_ago": 3,
                },
            ],
        },
    },
    {
        "id": "psp_a_venkataraman",
        "company": {
            "id": "co_quillstream",
            "name": "Quillstream",
            "domain": "quillstream.com",
            "industry": "Content operations SaaS",
            "headcount": 140,
            "hq_location": "Denver, CO",
            "funding_stage": "Series B",
            "enrichment": {
                "engineers": 58,
                "arr_estimate_usd": "17M",
                "repos": "GitHub, 46 active repositories",
                "stack": ["Python", "TypeScript", "GCP"],
                "last_round": {"amount_usd": "24M", "date": "2026-07-09", "lead": "Cascade Point Capital"},
            },
        },
        "full_name": "Priya Venkataraman",
        "designation": "VP Engineering",
        "seniority": "VP",
        "email": "priya.v@quillstream.com",
        "phone": None,
        "linkedin_url": "https://www.linkedin.com/in/priyavenkataraman-eng",
        "persona_match": "VP Eng doubling the team post-Series B",
        "source": "Funding announcement + LinkedIn",
        "stage": "contacted",
        "status": "in_outreach",
        "fit_score": 82.0,
        "fit_verdict": "qualified",
        "fit_criteria": _fit(88, 85, 76, 100, 81),
        "fit_rationale": (
            "Series B content operations SaaS in Denver with 58 engineers. Company size sits at "
            "the lower-middle of the band, which is fine, and the engineering count clears the "
            "25-engineer floor comfortably. Venkataraman is VP Engineering and the hiring signal "
            "is fresh: they are adding 18 engineers against a 58-person base, which is exactly "
            "the point at which review capacity stops scaling. Qualified."
        ),
        "research": {
            "company": {
                "name": "Quillstream",
                "what_they_do": "Content operations and editorial workflow for marketing teams.",
                "engineering_org": "58 engineers, growing to roughly 76 by Q1 2027.",
                "delivery_signals": "Eighteen open engineering roles posted since the July round.",
            },
            "person": {
                "name": "Priya Venkataraman",
                "role": "VP Engineering, promoted internally in 2025.",
                "public_writing": "No public writing found.",
                "likely_priorities": "Onboarding velocity, keeping the quality bar while the team grows.",
            },
            "signals": ["funding:series-b", "hiring:eighteen-roles"],
            "contact_channels": {"email": "verified", "linkedin": "active", "phone": "not found"},
            "inferred_pain_points": [
                "Team is growing 30% in two quarters with no change to review capacity",
                "Onboarding time to first merged PR will stretch as seniors absorb review load",
            ],
            "confidence": 0.72,
            "missing_fields": ["direct_phone", "public_writing"],
        },
        "signals": [
            {
                "signal_type": "hiring",
                "summary": "Posted 18 engineering roles since July, a 31% increase against a 58-person engineering team.",
                "source_url": "https://quillstream.com/careers",
                "days_ago": 17,
                "confidence": 0.83,
            },
            {
                "signal_type": "funding",
                "summary": "Closed a $24M Series B led by Cascade Point Capital, earmarked in the announcement for engineering and platform.",
                "source_url": "https://quillstream.com/blog/series-b",
                "days_ago": 73,
                "confidence": 0.91,
            },
        ],
        "follow_up_count": 1,
        "last_contacted_days": 2,
        "next_action_days": 3,
        "current_channel": "email",
        "channels_tried": ["email"],
        "thread": {
            "channel": "email",
            "status": "awaiting_reply",
            "sentiment": "neutral",
            "messages": [
                {
                    "direction": "outbound",
                    "message_type": "initial_outreach",
                    "subject": "18 open roles against 58 engineers",
                    "body": (
                        "Priya — you have 18 engineering roles open against a team of 58. Most VPs I "
                        "talk to find that the review queue, not hiring, is what actually caps "
                        "throughput at that ratio: the same six seniors end up reviewing for everyone "
                        "new.\n\n"
                        "Flightdeck reviews every pull request with a model that has read the whole "
                        "repository, so a new engineer gets a substantive first pass in minutes and "
                        "your seniors see the PR already cleaned up.\n\n"
                        "Is onboarding-to-first-merge something you are measuring right now?"
                    ),
                    "status": "delivered",
                    "days_ago": 9,
                    "confidence": 0.79,
                    "evidence_signal": 0,
                },
                {
                    "direction": "outbound",
                    "message_type": "follow_up",
                    "subject": "Re: 18 open roles against 58 engineers",
                    "body": (
                        "Following up once, then I will leave it.\n\n"
                        "The number worth knowing before the new cohort lands is median time from "
                        "pull request opened to first substantive review comment. If it is already "
                        "over a day, it roughly doubles as the team grows, and that is a much harder "
                        "problem to fix with people in the seat than before them.\n\n"
                        "Happy to send the two-page version rather than take a call."
                    ),
                    "status": "sent",
                    "days_ago": 2,
                    "confidence": 0.74,
                    "evidence_signal": 0,
                },
            ],
        },
    },
    {
        "id": "psp_a_sousa",
        "company": {
            "id": "co_cobaltyard",
            "name": "Cobalt Yard",
            "domain": "cobaltyard.com",
            "industry": "Supply chain visibility SaaS",
            "headcount": 205,
            "hq_location": "Chicago, IL",
            "funding_stage": "Series C",
            "enrichment": {
                "engineers": 84,
                "arr_estimate_usd": "33M",
                "repos": "GitLab self-managed",
                "stack": ["Elixir", "TypeScript", "Postgres", "AWS"],
            },
        },
        "full_name": "Ana Sousa",
        "designation": "VP Engineering",
        "seniority": "VP",
        "email": "ana.sousa@cobaltyard.com",
        "phone": None,
        "linkedin_url": "https://www.linkedin.com/in/anasousa-vpe",
        "persona_match": "VP Eng consolidating tooling spend",
        "source": "Conference talk + Apollo",
        "stage": "contacted",
        "status": "in_outreach",
        "fit_score": 78.0,
        "fit_verdict": "qualified",
        "fit_criteria": _fit(85, 85, 82, 100, 62),
        "fit_rationale": (
            "Series C supply chain SaaS, 84 engineers, Chicago. Firmly inside the ICP on "
            "industry, size, seniority and geography. The weak dimension is signal strength: "
            "the only dated evidence is a conference talk about tooling consolidation, which "
            "is a budget signal rather than a pain signal. Qualified on fundamentals, but the "
            "first touch has thin material to work with."
        ),
        "research": {
            "company": {
                "name": "Cobalt Yard",
                "what_they_do": "Real-time freight and inventory visibility for mid-market shippers.",
                "engineering_org": "84 engineers on self-managed GitLab, strong Elixir bias.",
                "delivery_signals": "Publicly discussing consolidating their developer tooling estate.",
            },
            "person": {
                "name": "Ana Sousa",
                "role": "VP Engineering since 2024.",
                "public_writing": "Spoke at ChicagoDevCon 2026 on tool sprawl.",
                "likely_priorities": "Reducing the number of vendors, self-managed deployment.",
            },
            "signals": ["conference_talk:tool-consolidation"],
            "contact_channels": {"email": "verified", "linkedin": "active", "phone": "not found"},
            "inferred_pain_points": [
                "Paying for overlapping code quality, SAST and analytics tools",
                "Self-managed GitLab means any vendor has to support non-cloud deployment",
            ],
            "confidence": 0.61,
            "missing_fields": ["review_latency_data", "incident_history", "direct_phone"],
        },
        "signals": [
            {
                "signal_type": "conference_talk",
                "summary": "Sousa's ChicagoDevCon talk described cutting the developer tooling estate from 11 vendors to 6 during 2026.",
                "source_url": "https://chicagodevcon.example.com/2026/sessions/tool-sprawl-sousa",
                "days_ago": 44,
                "confidence": 0.71,
            },
        ],
        "follow_up_count": 0,
        "last_contacted_days": 1,
        "next_action_days": 3,
        "current_channel": "email",
        "channels_tried": ["email"],
        "thread": {
            "channel": "email",
            "status": "awaiting_reply",
            "sentiment": "neutral",
            "messages": [
                {
                    "direction": "outbound",
                    "message_type": "initial_outreach",
                    "subject": "Eleven vendors down to six",
                    "body": (
                        "Ana — your ChicagoDevCon talk on going from eleven developer tools to six was "
                        "a refreshing change from the usual conference material, particularly the "
                        "point that every tool costs more in attention than in licence fees.\n\n"
                        "Flightdeck is usually a consolidation rather than an addition: review, "
                        "delivery analytics and AI-code policy in one platform, deployable on "
                        "self-managed GitLab inside your own account. Median customer retires 2.4 "
                        "tools in year one.\n\n"
                        "Which two are you still trying to get rid of?"
                    ),
                    "status": "delivered",
                    "days_ago": 1,
                    # Below the 0.62 threshold on this campaign: the draft that
                    # follows this one is sitting in the approvals queue.
                    "confidence": 0.58,
                    "evidence_signal": 0,
                },
            ],
            "pending_draft": {
                "message_type": "follow_up",
                "subject": "Re: Eleven vendors down to six",
                "body": (
                    "Ana — one more thought and then I will stop.\n\n"
                    "The consolidation case usually falls apart on deployment: most review tools are "
                    "SaaS-only, which is a non-starter when your GitLab is self-managed for a reason. "
                    "Flightdeck runs in your account, and on Enterprise it runs with self-hosted "
                    "inference, so nothing leaves your perimeter at all.\n\n"
                    "If it helps, I can send the deployment architecture rather than a deck."
                ),
                "status": "pending_approval",
                "confidence": 0.51,
                "hours_ago": 4,
            },
        },
    },
    {
        "id": "psp_a_raghavan",
        "company": {
            "id": "co_silverline_us",
            "name": "Silverline Fintech",
            "domain": "silverlinefin.com",
            "industry": "Payments and lending infrastructure SaaS",
            "headcount": 470,
            "hq_location": "New York, NY",
            "funding_stage": "Series D",
            "enrichment": {
                "engineers": 240,
                "arr_estimate_usd": "96M",
                "entity_resolved_as": "US parent entity, Delaware C-corp",
                "note": "India engineering centre counted separately by the BFSI campaign's research pass.",
                "stack": ["Java", "Go", "Kafka", "AWS"],
            },
        },
        "full_name": "Nikhil Raghavan",
        "designation": "Chief Technology Officer",
        "seniority": "C-level",
        "email": "nikhil.raghavan@silverlinefin.com",
        "phone": "+1-646-555-0119",
        "linkedin_url": "https://www.linkedin.com/in/nikhilraghavan-cto",
        "persona_match": "Fintech infrastructure CTO, Series D",
        "source": "Apollo + engineering blog",
        "stage": "contacted",
        "status": "in_outreach",
        "fit_score": 84.0,
        "fit_verdict": "qualified",
        "fit_criteria": _fit(88, 100, 78, 95, 74),
        "fit_rationale": (
            "Series D payments infrastructure company headquartered in New York with 240 "
            "engineers in the US entity. Raghavan is CTO and owns the platform roadmap. Fit is "
            "strong on every dimension; the only note is that Silverline's India engineering "
            "centre makes this company resolvable as an Indian BFSI entity too, so the identity "
            "key here is shared with another campaign and this record is the one that made "
            "contact first."
        ),
        "research": {
            "company": {
                "name": "Silverline Fintech",
                "what_they_do": "Embedded payments and lending rails for vertical SaaS platforms.",
                "engineering_org": "240 engineers in the US entity, plus a Bengaluru centre.",
                "delivery_signals": "Migrating the ledger service off a shared monolith through 2026.",
            },
            "person": {
                "name": "Nikhil Raghavan",
                "role": "CTO since 2022, co-led the ledger re-architecture.",
                "public_writing": "Silverline engineering blog on ledger correctness testing.",
                "likely_priorities": "Migration safety, audit trail, engineering cost per transaction.",
            },
            "signals": ["engineering_blog:ledger-migration", "expansion:bengaluru-centre"],
            "contact_channels": {"email": "verified", "linkedin": "active", "phone": "direct dial"},
            "inferred_pain_points": [
                "A ledger migration where a missed caller is a financial correctness bug",
                "Two engineering sites with different review cultures and no shared metric",
            ],
            "confidence": 0.85,
            "missing_fields": [],
        },
        "signals": [
            {
                "signal_type": "engineering_blog",
                "summary": "Silverline published a deep dive on extracting their ledger service from the monolith, describing manual cross-service review as the slowest part of each cut-over.",
                "source_url": "https://silverlinefin.com/engineering/ledger-extraction-part-two",
                "days_ago": 27,
                "confidence": 0.9,
            },
            {
                "signal_type": "expansion",
                "summary": "Opened a Bengaluru engineering centre and is hiring 40 platform engineers there through the first half of 2027.",
                "source_url": "https://silverlinefin.com/newsroom/bengaluru-engineering-centre",
                "days_ago": 52,
                "confidence": 0.93,
            },
        ],
        "follow_up_count": 0,
        "last_contacted_days": 5,
        "next_action_days": 2,
        "current_channel": "email",
        "channels_tried": ["email"],
        "thread": {
            "channel": "email",
            "status": "awaiting_reply",
            "sentiment": "neutral",
            "messages": [
                {
                    "direction": "outbound",
                    "message_type": "initial_outreach",
                    "subject": "The slow part of each ledger cut-over",
                    "body": (
                        "Nikhil — part two of your ledger extraction write-up made the point that the "
                        "slowest step in each cut-over is the manual cross-service review, because "
                        "nobody can hold every caller in their head.\n\n"
                        "That is the specific thing Flightdeck indexes for. It reads the whole "
                        "repository rather than the diff, so the review comment tells you which three "
                        "callers you missed, with the line numbers. On a ledger migration that is a "
                        "correctness argument, not a velocity one.\n\n"
                        "Is the cut-over sequence still running into 2027?"
                    ),
                    "status": "delivered",
                    "days_ago": 5,
                    "confidence": 0.86,
                    "evidence_signal": 0,
                },
            ],
        },
    },
    {
        "id": "psp_a_wexler",
        "company": {
            "id": "co_pinewell",
            "name": "Pinewell Retail Cloud",
            "domain": "pinewell.io",
            "industry": "Retail and point-of-sale SaaS",
            "headcount": 430,
            "hq_location": "Atlanta, GA",
            "funding_stage": "Series D",
            "enrichment": {
                "engineers": 175,
                "arr_estimate_usd": "74M",
                "repos": "GitHub Enterprise",
                "stack": ["C#", ".NET", "TypeScript", "Azure"],
                "opportunity_value_usd": 214000,
            },
        },
        "full_name": "Sharon Wexler",
        "designation": "SVP Engineering",
        "seniority": "SVP",
        "email": "sharon.wexler@pinewell.io",
        "phone": "+1-404-555-0173",
        "linkedin_url": "https://www.linkedin.com/in/sharonwexler",
        "persona_match": "SVP Eng, multi-team, peak-season reliability",
        "source": "Referral from Hollowell Commerce",
        "stage": "opportunity",
        "status": "meeting_booked",
        "fit_score": 93.0,
        "fit_verdict": "qualified",
        "fit_criteria": _fit(90, 95, 86, 100, 94),
        "fit_rationale": (
            "The strongest record in the campaign. Referred in by an existing customer, which "
            "removes most of the qualification risk, and the timing signal is exact: Pinewell "
            "freezes changes from November for peak retail season, so the buying window closes "
            "in eight weeks. Wexler is SVP, above the seniority gate, so the first touch went "
            "through human approval before it sent."
        ),
        "research": {
            "company": {
                "name": "Pinewell Retail Cloud",
                "what_they_do": "Point-of-sale and inventory cloud for multi-site specialty retailers.",
                "engineering_org": "175 engineers across six product groups on Azure.",
                "delivery_signals": "Annual change freeze from 1 November through early January.",
            },
            "person": {
                "name": "Sharon Wexler",
                "role": "SVP Engineering, 4 years in seat, owns platform and infrastructure budget.",
                "public_writing": "None found.",
                "likely_priorities": "Peak-season reliability, budget cycle closing in Q4.",
            },
            "signals": ["referral:hollowell", "seasonality:change-freeze", "hiring:sdet-backfill"],
            "contact_channels": {"email": "verified", "linkedin": "active", "phone": "direct dial"},
            "inferred_pain_points": [
                "Everything that ships must land before the November freeze",
                "Regression risk during the pre-freeze crunch is their historical incident peak",
            ],
            "confidence": 0.92,
            "missing_fields": [],
        },
        "signals": [
            {
                "signal_type": "referral",
                "summary": "Introduced by Hollowell Commerce's VP Engineering, who cited Flightdeck's review latency result when recommending the call.",
                "source_url": "https://crm.corvuslabs.ai/referrals/hollowell-pinewell",
                "days_ago": 34,
                "confidence": 0.97,
            },
            {
                "signal_type": "seasonality",
                "summary": "Pinewell's customer release notes confirm an annual platform change freeze running 1 November to 6 January.",
                "source_url": "https://pinewell.io/release-notes/2025-freeze-window",
                "days_ago": 29,
                "confidence": 0.88,
            },
        ],
        "follow_up_count": 2,
        "last_contacted_days": 4,
        "next_action_days": 3,
        "current_channel": "email",
        "channels_tried": ["email", "voice"],
        "thread": {
            "channel": "email",
            "status": "meeting_booked",
            "sentiment": "positive",
            "messages": [
                {
                    "direction": "outbound",
                    "message_type": "initial_outreach",
                    "subject": "Intro from Hollowell — before the November freeze",
                    "body": (
                        "Sharon — Jen at Hollowell suggested I write. Her team cut review latency from "
                        "19 hours to 7 with Flightdeck and she thought the shape of your problem was "
                        "similar.\n\n"
                        "The reason I am writing now rather than in October: your release notes put "
                        "the platform change freeze at 1 November, which means anything that has to "
                        "prove itself before peak needs to be in a repo by early October.\n\n"
                        "Would a 25-minute technical session in the next fortnight be useful, or is "
                        "the pre-freeze crunch already too full?"
                    ),
                    "status": "replied",
                    "days_ago": 26,
                    "confidence": 0.91,
                    "evidence_signal": 0,
                },
                {
                    "direction": "inbound",
                    "message_type": "reply",
                    "subject": "Re: Intro from Hollowell — before the November freeze",
                    "body": (
                        "Jen mentioned you. The freeze date is right and yes, October is the wall.\n\n"
                        "Set up the technical session. I want my platform lead and our security "
                        "architect on it — we are on Azure and anything touching source has to clear "
                        "an internal review, so bring the deployment detail, not the deck."
                    ),
                    "status": "delivered",
                    "days_ago": 24,
                },
                {
                    "direction": "outbound",
                    "message_type": "follow_up",
                    "subject": "Re: Intro from Hollowell — before the November freeze",
                    "body": (
                        "Booked for Tuesday. Sending the Azure reference architecture, the SOC 2 Type "
                        "II report and our DPA ahead of it so your security architect has something to "
                        "mark up rather than react to live.\n\n"
                        "Short version for the call: private VPC in your own subscription, no code "
                        "egress, contractual no-training on your repositories. The interesting "
                        "question is whether we can show a measurable change before 1 November, and "
                        "I think we can on two repos."
                    ),
                    "status": "delivered",
                    "days_ago": 4,
                    "confidence": 0.89,
                    "evidence_signal": 1,
                },
            ],
        },
    },
    {
        "id": "psp_a_lindqvist",
        "company": {
            "id": "co_tidemark",
            "name": "Tidemark Health",
            "domain": "tidemarkhealth.com",
            "industry": "Healthcare operations SaaS",
            "headcount": 260,
            "hq_location": "Nashville, TN",
            "funding_stage": "Series C",
            "enrichment": {
                "engineers": 102,
                "arr_estimate_usd": "38M",
                "compliance": ["HIPAA", "HITRUST"],
                "stack": ["Ruby", "TypeScript", "AWS"],
            },
        },
        "full_name": "Rebecca Lindqvist",
        "designation": "VP Platform Engineering",
        "seniority": "VP",
        "email": "rebecca.lindqvist@tidemarkhealth.com",
        "phone": None,
        "linkedin_url": "https://www.linkedin.com/in/rebeccalindqvist",
        "persona_match": "First VP Platform, HIPAA-regulated",
        "source": "Job posting + Apollo",
        "stage": "qualified",
        "status": "awaiting_approval",
        "fit_score": 80.0,
        "fit_verdict": "qualified",
        "fit_criteria": _fit(82, 88, 84, 100, 68),
        "fit_rationale": (
            "Series C healthcare SaaS, 102 engineers, Nashville. Industry scores slightly lower "
            "because HIPAA and HITRUST lengthen every technical evaluation, but that same "
            "constraint is the reason Guard's provenance trail matters to them. Lindqvist was "
            "hired six weeks ago into a newly created VP Platform Engineering role, which is the "
            "cleanest buying trigger in this campaign's playbook. Held at the seniority gate."
        ),
        "research": {
            "company": {
                "name": "Tidemark Health",
                "what_they_do": "Scheduling and capacity operations for hospital networks.",
                "engineering_org": "102 engineers; platform function created in 2026.",
                "delivery_signals": "Hired a first VP Platform Engineering with a developer experience remit.",
            },
            "person": {
                "name": "Rebecca Lindqvist",
                "role": "VP Platform Engineering, six weeks in seat, previously at a claims platform.",
                "public_writing": "None found.",
                "likely_priorities": "Establishing a baseline, proving the new function's value in 90 days.",
            },
            "signals": ["leadership_change:first-vp-platform", "hiring:dx-engineer"],
            "contact_channels": {"email": "verified", "linkedin": "active", "phone": "not found"},
            "inferred_pain_points": [
                "New function with no measured baseline to justify its roadmap",
                "HIPAA scope means any tool touching code needs a documented review",
            ],
            "confidence": 0.78,
            "missing_fields": ["direct_phone", "current_tooling"],
        },
        "signals": [
            {
                "signal_type": "leadership_change",
                "summary": "Tidemark created its first VP Platform Engineering role and filled it with Lindqvist in August, with developer experience named in the posting.",
                "source_url": "https://www.linkedin.com/posts/tidemarkhealth-vp-platform-engineering",
                "days_ago": 42,
                "confidence": 0.87,
            },
            {
                "signal_type": "hiring",
                "summary": "Follow-on posting for two Developer Experience Engineers reporting into the new platform function.",
                "source_url": "https://tidemarkhealth.com/careers/developer-experience-engineer",
                "days_ago": 15,
                "confidence": 0.8,
            },
        ],
        "follow_up_count": 0,
        "last_contacted_days": None,
        "next_action_hours": 6,
        "current_channel": None,
        "channels_tried": [],
        "thread": None,
    },
    {
        "id": "psp_a_ramirez",
        "company": {
            "id": "co_fernpost",
            "name": "Fernpost",
            "domain": "fernpost.com",
            "industry": "Field service management SaaS",
            "headcount": 95,
            "hq_location": "Portland, OR",
            "funding_stage": "Series B",
            "enrichment": {
                "engineers": 31,
                "arr_estimate_usd": "11M",
                "stack": ["Python", "React Native", "GCP"],
            },
        },
        "full_name": "Joel Ramirez",
        "designation": "Chief Technology Officer",
        "seniority": "C-level",
        "email": "joel@fernpost.com",
        "phone": None,
        "linkedin_url": "https://www.linkedin.com/in/joelramirez-fernpost",
        "persona_match": "Small-team CTO, borderline on engineering size",
        "source": "Apollo",
        "stage": "researched",
        "status": "awaiting_approval",
        "fit_score": 58.0,
        "fit_verdict": "borderline",
        "fit_criteria": _fit(80, 100, 42, 100, 38),
        "fit_rationale": (
            "Genuinely borderline, which is why it is with a human. Industry, seniority and "
            "geography are all clean: Series B field service SaaS in Portland with the CTO as "
            "the contact. The problem is size and evidence. Thirty-one engineers is six above "
            "our floor, so the review-capacity argument is weak, and the only dated signal is a "
            "generic careers page refresh. Recommend rejecting unless a stronger signal appears "
            "in the next 30 days."
        ),
        "research": {
            "company": {
                "name": "Fernpost",
                "what_they_do": "Dispatch and job management for residential field service businesses.",
                "engineering_org": "31 engineers, single product team, mobile-heavy.",
                "delivery_signals": "No public delivery or incident data found.",
            },
            "person": {
                "name": "Joel Ramirez",
                "role": "Co-founder and CTO.",
                "public_writing": "None found.",
                "likely_priorities": "Mobile release cadence, keeping the team small.",
            },
            "signals": ["hiring:careers-refresh"],
            "contact_channels": {"email": "pattern-matched, unverified", "linkedin": "active"},
            "inferred_pain_points": ["Unclear — insufficient public evidence"],
            "confidence": 0.44,
            "missing_fields": ["review_latency_data", "incident_history", "verified_email", "tooling"],
        },
        "signals": [
            {
                "signal_type": "hiring",
                "summary": "Careers page refreshed with three engineering roles, none of them platform or developer experience.",
                "source_url": "https://fernpost.com/careers",
                "days_ago": 21,
                "confidence": 0.52,
            },
        ],
        "follow_up_count": 0,
        "last_contacted_days": None,
        "next_action_hours": 20,
        "current_channel": None,
        "channels_tried": [],
        "thread": None,
    },
    {
        "id": "psp_a_grant",
        "company": {
            "id": "co_halyard",
            "name": "Halyard Security",
            "domain": "halyard.dev",
            "industry": "Cloud security SaaS",
            "headcount": 120,
            "hq_location": "Seattle, WA",
            "funding_stage": "Series B",
            "enrichment": {"engineers": 54, "stack": ["Rust", "Go", "Kubernetes"]},
        },
        "full_name": "Tobias Grant",
        "designation": "Chief Technology Officer",
        "seniority": "C-level",
        "email": None,
        "phone": None,
        "linkedin_url": "https://www.linkedin.com/in/tobiasgrant-halyard",
        "persona_match": "Series B security CTO",
        "source": "Prospect Generation Agent, overnight batch",
        "stage": "discovered",
        "status": "pending",
        "fit_score": None,
        "fit_verdict": None,
        "fit_criteria": {},
        "fit_rationale": None,
        "research": {},
        "signals": [],
        "follow_up_count": 0,
        "last_contacted_days": None,
        "next_action_hours": 2,
        "current_channel": None,
        "channels_tried": [],
        "thread": None,
    },
    {
        "id": "psp_a_nakamura",
        "company": {
            "id": "co_ledgerloop",
            "name": "Ledgerloop",
            "domain": "ledgerloop.com",
            "industry": "Expense management SaaS",
            "headcount": 34,
            "hq_location": "San Francisco, CA",
            "funding_stage": "Seed",
            "enrichment": {"engineers": 12, "stack": ["TypeScript", "Postgres"]},
        },
        "full_name": "Curtis Nakamura",
        "designation": "Director of Engineering",
        "seniority": "Director",
        "email": "curtis@ledgerloop.com",
        "phone": None,
        "linkedin_url": "https://www.linkedin.com/in/curtisnakamura",
        "persona_match": "Outside ICP on stage and size",
        "source": "Apollo",
        "stage": "researched",
        "status": "rejected",
        "fit_score": 31.0,
        "fit_verdict": "rejected",
        "fit_criteria": _fit(78, 45, 8, 100, 24),
        "fit_rationale": (
            "Rejected on two independent exclusion rules. Ledgerloop is seed-stage with twelve "
            "engineers, less than half the 25-engineer floor and two rounds short of the Series "
            "B minimum, so the review-capacity argument has nothing to bite on. Nakamura is a "
            "Director rather than a VP or C-level, which puts him below the buying authority "
            "this campaign targets. No amount of signal strength would change this verdict."
        ),
        "research": {
            "company": {
                "name": "Ledgerloop",
                "what_they_do": "Expense management for early-stage startups.",
                "engineering_org": "12 engineers, one team.",
                "delivery_signals": "None relevant at this size.",
            },
            "person": {"name": "Curtis Nakamura", "role": "Director of Engineering, first engineering manager hire."},
            "signals": [],
            "contact_channels": {"email": "verified", "linkedin": "active"},
            "inferred_pain_points": ["Not applicable — outside ICP"],
            "confidence": 0.83,
            "missing_fields": [],
        },
        "signals": [
            {
                "signal_type": "funding",
                "summary": "Announced a $4.5M seed extension, two rounds below this campaign's Series B floor.",
                "source_url": "https://ledgerloop.com/blog/seed-extension",
                "days_ago": 61,
                "confidence": 0.79,
            },
        ],
        "follow_up_count": 0,
        "last_contacted_days": None,
        "next_action_hours": None,
        "current_channel": None,
        "channels_tried": [],
        "thread": None,
    },
]


PROSPECTS_B: list[dict] = [
    {
        "id": "psp_b_venkatesan",
        "company": {
            "id": "co_anvaya",
            "name": "Anvaya Bank",
            "domain": "anvayabank.in",
            "industry": "Private sector bank",
            "headcount": 14200,
            "hq_location": "Mumbai, Maharashtra",
            "funding_stage": "Listed",
            "enrichment": {
                "in_house_engineers": 620,
                "core_banking": "Finacle",
                "branches": 1140,
                "regulator": "Reserve Bank of India",
                "cloud_posture": "Private cloud, ap-south-1 for analytics workloads",
            },
        },
        "full_name": "Ramesh Venkatesan",
        "designation": "Chief Information Officer",
        "seniority": "CXO",
        "email": "ramesh.venkatesan@anvayabank.in",
        "phone": None,
        "linkedin_url": "https://www.linkedin.com/in/rameshvenkatesan-cio",
        "persona_match": "Bank CIO running a core modernisation programme",
        "source": "Annual report + regulatory filing",
        "stage": "contacted",
        "status": "in_outreach",
        "fit_score": 89.0,
        "fit_verdict": "qualified",
        "fit_criteria": _fit(100, 95, 96, 100, 84),
        "fit_rationale": (
            "Close to ideal for this campaign. Anvaya is a listed private sector bank with "
            "14,200 employees and, unusually for this segment, 620 in-house engineers rather "
            "than a fully outsourced estate, which is the exclusion that removes most Indian "
            "banks from the list. Venkatesan is the CIO and the named sponsor of a multi-year "
            "core modernisation programme disclosed in the annual report. Approval gate applies "
            "on CXO seniority."
        ),
        "research": {
            "company": {
                "name": "Anvaya Bank",
                "what_they_do": "Private sector retail and SME bank, 1,140 branches.",
                "engineering_org": "620 in-house engineers in Mumbai and Hyderabad, Finacle core.",
                "delivery_signals": "Three-year core modernisation programme disclosed to shareholders.",
            },
            "person": {
                "name": "Ramesh Venkatesan",
                "role": "CIO since 2021, sponsor of the modernisation programme.",
                "public_writing": "Panel appearance at the IBA Banking Technology Conference.",
                "likely_priorities": "RBI IT governance compliance, programme delivery risk, vendor concentration.",
            },
            "signals": ["regulatory:rbi-governance", "expansion:modernisation-programme", "hiring:platform-engineers"],
            "contact_channels": {"email": "verified via corporate directory", "linkedin": "active"},
            "inferred_pain_points": [
                "A three-year core programme where change control is a regulatory obligation, not a preference",
                "Auditable provenance for AI-assisted code before the 2027 inspection cycle",
                "620 engineers with no common delivery metric across Mumbai and Hyderabad",
            ],
            "confidence": 0.86,
            "missing_fields": ["direct_phone"],
        },
        "signals": [
            {
                "signal_type": "regulatory",
                "summary": "Anvaya's FY26 annual report discloses an RBI observation on change management controls in application development and commits to remediation by March 2027.",
                "source_url": "https://anvayabank.in/investors/annual-report-fy26.pdf",
                "days_ago": 68,
                "confidence": 0.92,
            },
            {
                "signal_type": "expansion",
                "summary": "Board approved a three-year core banking modernisation programme with an in-house engineering build rather than a systems integrator.",
                "source_url": "https://anvayabank.in/investors/board-outcome-2026-07",
                "days_ago": 55,
                "confidence": 0.89,
            },
            {
                "signal_type": "hiring",
                "summary": "Posted 26 platform and DevSecOps engineer roles in Hyderabad, all specifying on-premise GitLab experience.",
                "source_url": "https://anvayabank.in/careers/technology",
                "days_ago": 30,
                "confidence": 0.81,
            },
        ],
        "follow_up_count": 1,
        "last_contacted_days": 9,
        "next_action_days": None,
        "current_channel": "email",
        "channels_tried": ["email", "linkedin"],
        "thread": {
            "channel": "email",
            "status": "awaiting_reply",
            "sentiment": "neutral",
            "messages": [
                {
                    "direction": "outbound",
                    "message_type": "initial_outreach",
                    "subject": "Change management remediation before March 2027",
                    "body": (
                        "Mr Venkatesan — Anvaya's FY26 annual report commits to remediating the RBI "
                        "observation on application change management controls by March 2027, and the "
                        "board has approved building the core programme in house rather than through "
                        "an integrator. Those two facts together make the evidence trail for every "
                        "code change a programme deliverable.\n\n"
                        "Corvus Flightdeck records who or what wrote each change, what reviewed it and "
                        "on what basis, entirely inside your own data centre or ap-south-1, with "
                        "customer-managed keys.\n\n"
                        "If it is useful I can send the control-mapping document your audit team would "
                        "need, rather than a product overview."
                    ),
                    "status": "delivered",
                    "days_ago": 16,
                    "confidence": 0.87,
                    "evidence_signal": 0,
                },
                {
                    "direction": "outbound",
                    "message_type": "follow_up",
                    "subject": "Re: Change management remediation before March 2027",
                    "body": (
                        "Following up once. I have attached nothing and am not asking for a meeting.\n\n"
                        "The one question worth putting to your programme team is whether the current "
                        "toolchain can answer, for any line in the core build, which model or engineer "
                        "produced it and which review approved it. If the answer needs a manual "
                        "reconstruction, that is the gap we close.\n\n"
                        "Happy to send the control mapping to whoever owns the remediation evidence."
                    ),
                    "status": "delivered",
                    "days_ago": 9,
                    "confidence": 0.84,
                    "evidence_signal": 0,
                },
            ],
        },
    },
    {
        "id": "psp_b_krishnan",
        "company": {
            "id": "co_sanchay",
            "name": "Sanchay Life Insurance",
            "domain": "sanchaylife.in",
            "industry": "Life insurance",
            "headcount": 9800,
            "hq_location": "Pune, Maharashtra",
            "funding_stage": "Listed subsidiary",
            "enrichment": {
                "in_house_engineers": 310,
                "policy_admin": "In-house Java platform",
                "regulator": "IRDAI",
                "cloud_posture": "Hybrid, ap-south-1",
            },
        },
        "full_name": "Deepa Krishnan",
        "designation": "Head of Digital Technology",
        "seniority": "Head",
        "email": "deepa.krishnan@sanchaylife.in",
        "phone": None,
        "linkedin_url": "https://www.linkedin.com/in/deepakrishnan-digital",
        "persona_match": "Insurance technology head with an in-house build team",
        "source": "IRDAI circular response + LinkedIn",
        "stage": "engaged",
        "status": "escalated",
        "fit_score": 83.0,
        "fit_verdict": "qualified",
        "fit_criteria": _fit(95, 82, 92, 100, 76),
        "fit_rationale": (
            "Insurer with 9,800 employees and, importantly, a policy administration platform "
            "built and maintained in house by 310 engineers rather than licensed. That is what "
            "makes them addressable: our exclusion rule removes institutions that have "
            "outsourced application development, and Sanchay is the opposite case. Krishnan "
            "sits one level below CXO, which costs a few points on seniority but makes her the "
            "practical evaluator."
        ),
        "research": {
            "company": {
                "name": "Sanchay Life Insurance",
                "what_they_do": "Life and annuity products sold through bancassurance and agency.",
                "engineering_org": "310 engineers in Pune maintaining an in-house policy admin platform.",
                "delivery_signals": "Publicly responded to the IRDAI cyber security guidelines consultation.",
            },
            "person": {
                "name": "Deepa Krishnan",
                "role": "Head of Digital Technology, reports to the CIO.",
                "public_writing": "Co-authored Sanchay's response to the IRDAI consultation paper.",
                "likely_priorities": "IRDAI compliance evidence, cost per release, in-house team productivity.",
            },
            "signals": ["regulatory:irdai-consultation", "hiring:java-modernisation"],
            "contact_channels": {"email": "verified", "linkedin": "active"},
            "inferred_pain_points": [
                "Evidence burden under the IRDAI information and cyber security guidelines",
                "A large in-house Java estate with uneven review standards across squads",
            ],
            "confidence": 0.79,
            "missing_fields": ["direct_phone", "current_review_tooling"],
        },
        "signals": [
            {
                "signal_type": "regulatory",
                "summary": "Sanchay co-signed an industry response to the IRDAI information and cyber security consultation, specifically on evidence requirements for software change control.",
                "source_url": "https://sanchaylife.in/newsroom/irdai-consultation-response",
                "days_ago": 47,
                "confidence": 0.85,
            },
            {
                "signal_type": "hiring",
                "summary": "Recruiting 40 Java engineers in Pune for a policy administration modernisation track running into 2028.",
                "source_url": "https://sanchaylife.in/careers/technology-pune",
                "days_ago": 25,
                "confidence": 0.82,
            },
        ],
        "follow_up_count": 1,
        "last_contacted_days": 12,
        "next_action_days": None,
        "current_channel": "email",
        "channels_tried": ["email"],
        "thread": {
            "channel": "email",
            "status": "escalated",
            "sentiment": "neutral",
            "escalation_reason": "Prospect asked for enterprise pricing and a data residency commitment. Campaign policy escalates every pricing and compliance question to a human on first ask.",
            "messages": [
                {
                    "direction": "outbound",
                    "message_type": "initial_outreach",
                    "subject": "Evidence for software change control under the IRDAI guidelines",
                    "body": (
                        "Deepa — Sanchay's response to the IRDAI consultation argued that the evidence "
                        "burden for software change control should be proportionate to the change, "
                        "which is a more practical position than most of the industry took.\n\n"
                        "That is the problem we work on. Flightdeck records provenance and review "
                        "rationale for every change automatically, so the evidence exists whether or "
                        "not anyone remembers to produce it, and it runs inside your ap-south-1 "
                        "account with customer-managed keys.\n\n"
                        "With 40 Java engineers joining the modernisation track, is review capacity "
                        "already a constraint, or is it the evidence trail that worries you more?"
                    ),
                    "status": "replied",
                    "days_ago": 20,
                    "confidence": 0.88,
                    "evidence_signal": 0,
                },
                {
                    "direction": "inbound",
                    "message_type": "reply",
                    "subject": "Re: Evidence for software change control under the IRDAI guidelines",
                    "body": (
                        "Both, though the evidence trail is what my CIO asks about.\n\n"
                        "Two things before we go further. What does this cost for roughly 300 "
                        "developers — I need an indicative annual number, not a range with 'contact "
                        "us' at the end. And can you confirm in writing that no code, prompt or "
                        "metadata leaves Indian jurisdiction at any point, including for model "
                        "inference? We have been through this with two vendors and both failed on "
                        "the second question."
                    ),
                    "status": "delivered",
                    "days_ago": 12,
                },
            ],
        },
    },
    {
        "id": "psp_b_raghavan",
        "company": {
            "id": "co_silverline_in",
            "name": "Silverline Fintech India",
            "domain": "silverlinefin.com",
            "industry": "Payments infrastructure",
            "headcount": 1240,
            "hq_location": "Bengaluru, Karnataka",
            "funding_stage": "Series D",
            "enrichment": {
                "in_house_engineers": 780,
                "entity_resolved_as": "India entity including the Bengaluru engineering centre",
                "note": "The US parent is a separate record in US SaaS · CTO Outreach. Same human, same email.",
                "regulator": "RBI payment aggregator licence in process",
            },
        },
        "full_name": "Nikhil Raghavan",
        "designation": "Chief Technology Officer",
        "seniority": "CXO",
        "email": "nikhil.raghavan@silverlinefin.com",
        "phone": "+1-646-555-0119",
        "linkedin_url": "https://www.linkedin.com/in/nikhilraghavan-cto",
        "persona_match": "CTO of a payments entity with 1,000+ India headcount",
        "source": "RBI licence register + company site",
        "stage": "qualified",
        "status": "awaiting_approval",
        "fit_score": 76.0,
        "fit_verdict": "qualified",
        "fit_criteria": _fit(88, 95, 72, 60, 65),
        "fit_rationale": (
            "Qualifies on the letter of the ICP and is blocked on conflict rather than fit. "
            "Silverline's India entity has 1,240 employees and 780 engineers in Bengaluru, and "
            "it is pursuing an RBI payment aggregator licence, which puts it inside the BFSI "
            "definition. Geography scores low because the decision maker sits in New York, not "
            "India. The same person is already in active outreach under US SaaS · CTO Outreach, "
            "so the duplicate-outreach guardrail has held this record for a human to resolve."
        ),
        "research": {
            "company": {
                "name": "Silverline Fintech India",
                "what_they_do": "Embedded payments rails; India entity holds the engineering centre.",
                "engineering_org": "780 engineers in Bengaluru as of September 2026.",
                "delivery_signals": "Hiring 40 platform engineers; licence application in progress.",
            },
            "person": {
                "name": "Nikhil Raghavan",
                "role": "Group CTO, based in New York, owns the Bengaluru centre.",
                "public_writing": "Silverline engineering blog.",
                "likely_priorities": "Two-site consistency, licence readiness, migration safety.",
            },
            "signals": ["expansion:bengaluru-centre", "regulatory:pa-licence"],
            "contact_channels": {"email": "verified", "linkedin": "active", "phone": "US direct dial"},
            "inferred_pain_points": [
                "Two engineering sites, one codebase, no shared review standard",
                "Licence readiness will require documented change control",
            ],
            "confidence": 0.74,
            "missing_fields": ["india_based_decision_maker"],
        },
        "signals": [
            {
                "signal_type": "expansion",
                "summary": "Opened a Bengaluru engineering centre and is hiring 40 platform engineers there through the first half of 2027.",
                "source_url": "https://silverlinefin.com/newsroom/bengaluru-engineering-centre",
                "days_ago": 52,
                "confidence": 0.93,
            },
            {
                "signal_type": "regulatory",
                "summary": "Listed as an in-process applicant on the RBI payment aggregator register, which brings documented change control into scope.",
                "source_url": "https://www.rbi.org.in/scripts/pa-applicants-register",
                "days_ago": 39,
                "confidence": 0.78,
            },
        ],
        "follow_up_count": 0,
        "last_contacted_days": None,
        "next_action_hours": None,
        "current_channel": None,
        "channels_tried": [],
        "thread": None,
    },
    {
        "id": "psp_b_bhattacharya",
        "company": {
            "id": "co_trimurti",
            "name": "Trimurti Capital",
            "domain": "trimurticapital.in",
            "industry": "Non-banking financial company",
            "headcount": 3400,
            "hq_location": "Chennai, Tamil Nadu",
            "funding_stage": "Listed",
            "enrichment": {
                "in_house_engineers": 185,
                "lending_platform": "In-house, Java and Spring",
                "regulator": "Reserve Bank of India, upper layer NBFC",
            },
        },
        "full_name": "Sameer Bhattacharya",
        "designation": "Chief Technology Officer",
        "seniority": "CXO",
        "email": "sameer.bhattacharya@trimurticapital.in",
        "phone": None,
        "linkedin_url": "https://www.linkedin.com/in/sameerbhattacharya-cto",
        "persona_match": "NBFC CTO, upper-layer regulatory scope",
        "source": "RBI upper layer list + annual report",
        "stage": "qualified",
        "status": "in_outreach",
        "fit_score": 74.0,
        "fit_verdict": "qualified",
        "fit_criteria": _fit(92, 95, 70, 100, 58),
        "fit_rationale": (
            "Sits just above the qualification line. Trimurti is an upper-layer NBFC with 3,400 "
            "employees and an in-house lending platform, which satisfies both the size and the "
            "in-house engineering tests. What pulls the score down is signal strength: the only "
            "dated evidence is their inclusion in the RBI upper layer list, which is a scope "
            "change rather than a project. Worth a first touch, but expect a slow reply."
        ),
        "research": {
            "company": {
                "name": "Trimurti Capital",
                "what_they_do": "Secured SME and vehicle lending across south India.",
                "engineering_org": "185 engineers in Chennai maintaining an in-house lending platform.",
                "delivery_signals": "Moved into the RBI upper layer, which tightens IT governance obligations.",
            },
            "person": {
                "name": "Sameer Bhattacharya",
                "role": "CTO since 2023, previously at a core banking vendor.",
                "public_writing": "None found.",
                "likely_priorities": "Meeting upper-layer IT governance obligations without adding headcount.",
            },
            "signals": ["regulatory:upper-layer"],
            "contact_channels": {"email": "verified", "linkedin": "active"},
            "inferred_pain_points": [
                "New governance obligations landing on an unchanged engineering team",
                "In-house platform with no automated evidence of review",
            ],
            "confidence": 0.7,
            "missing_fields": ["direct_phone", "programme_detail"],
        },
        "signals": [
            {
                "signal_type": "regulatory",
                "summary": "Named in the RBI's upper layer NBFC list for 2026-27, which brings scale-based IT governance requirements into scope from April.",
                "source_url": "https://www.rbi.org.in/scripts/nbfc-upper-layer-2026-27",
                "days_ago": 84,
                "confidence": 0.9,
            },
        ],
        "follow_up_count": 0,
        "last_contacted_days": None,
        "next_action_hours": None,
        "current_channel": None,
        "channels_tried": [],
        "thread": None,
    },
    {
        "id": "psp_b_rajagopal",
        "company": {
            "id": "co_kaveri",
            "name": "Kaveri Gramin Bank",
            "domain": "kaverigraminbank.in",
            "industry": "Regional rural bank",
            "headcount": 6100,
            "hq_location": "Bengaluru, Karnataka",
            "funding_stage": "Government sponsored",
            "enrichment": {"in_house_engineers": 140, "core_banking": "Finacle", "branches": 720},
        },
        "full_name": "Lalitha Rajagopal",
        "designation": "Head of Information Technology",
        "seniority": "Head",
        "email": "lalitha.rajagopal@kaverigraminbank.in",
        "phone": None,
        "linkedin_url": "https://www.linkedin.com/in/lalitharajagopal",
        "persona_match": "IT head at a regional rural bank with a small in-house team",
        "source": "Bank tender portal",
        "stage": "researched",
        "status": "researching",
        "fit_score": None,
        "fit_verdict": None,
        "fit_criteria": {},
        "fit_rationale": None,
        "research": {
            "company": {
                "name": "Kaveri Gramin Bank",
                "what_they_do": "Regional rural bank serving 720 branches across Karnataka.",
                "engineering_org": "Approximately 140 in-house technology staff, mostly integration rather than product engineering.",
                "delivery_signals": "Open tender for a digital lending platform build.",
            },
            "person": {
                "name": "Lalitha Rajagopal",
                "role": "Head of Information Technology.",
                "public_writing": "None found.",
                "likely_priorities": "Tender delivery, vendor management.",
            },
            "signals": ["tender:digital-lending"],
            "contact_channels": {"email": "pattern-matched, unverified", "linkedin": "active"},
            "inferred_pain_points": ["Unclear whether development is in house or contracted to the tender winner"],
            "confidence": 0.51,
            "missing_fields": ["in_house_vs_outsourced_split", "verified_email", "decision_authority"],
        },
        "signals": [
            {
                "signal_type": "tender",
                "summary": "Published an open tender for a digital lending platform, with the build scope explicitly open to either in-house or vendor delivery.",
                "source_url": "https://kaverigraminbank.in/tenders/2026-digital-lending",
                "days_ago": 36,
                "confidence": 0.74,
            },
        ],
        "follow_up_count": 0,
        "last_contacted_days": None,
        "next_action_hours": None,
        "current_channel": None,
        "channels_tried": [],
        "thread": None,
    },
    {
        "id": "psp_b_pillai",
        "company": {
            "id": "co_prathama",
            "name": "Prathama General Insurance",
            "domain": "prathamagi.in",
            "industry": "General insurance",
            "headcount": 4500,
            "hq_location": "Hyderabad, Telangana",
            "funding_stage": "Private",
            "enrichment": {"in_house_engineers": 210, "regulator": "IRDAI"},
        },
        "full_name": "Arun Pillai",
        "designation": "Chief Information Officer",
        "seniority": "CXO",
        "email": None,
        "phone": None,
        "linkedin_url": "https://www.linkedin.com/in/arunpillai-cio",
        "persona_match": "General insurance CIO",
        "source": "Prospect Generation Agent, weekly batch",
        "stage": "discovered",
        "status": "pending",
        "fit_score": None,
        "fit_verdict": None,
        "fit_criteria": {},
        "fit_rationale": None,
        "research": {},
        "signals": [],
        "follow_up_count": 0,
        "last_contacted_days": None,
        "next_action_hours": None,
        "current_channel": None,
        "channels_tried": [],
        "thread": None,
    },
    {
        "id": "psp_b_qureshi",
        "company": {
            "id": "co_meridian_broking",
            "name": "Meridian Broking",
            "domain": "meridianbroking.in",
            "industry": "Retail broking",
            "headcount": 2100,
            "hq_location": "Mumbai, Maharashtra",
            "funding_stage": "Listed",
            "enrichment": {"in_house_engineers": 12, "development_model": "Fully outsourced to a managed services provider"},
        },
        "full_name": "Faisal Qureshi",
        "designation": "VP Technology",
        "seniority": "VP",
        "email": "faisal.qureshi@meridianbroking.in",
        "phone": None,
        "linkedin_url": "https://www.linkedin.com/in/faisalqureshi-tech",
        "persona_match": "Outside ICP — no in-house development",
        "source": "Exchange filings",
        "stage": "researched",
        "status": "rejected",
        "fit_score": 27.0,
        "fit_verdict": "rejected",
        "fit_criteria": _fit(85, 60, 65, 100, 20),
        "fit_rationale": (
            "Rejected on the campaign's clearest exclusion rule. Meridian meets the headcount "
            "test at 2,100 employees and sits squarely in capital markets, but its application "
            "development is contracted end to end to a managed services provider and it retains "
            "roughly twelve internal technology staff, all in vendor management. There is no "
            "in-house pull request to review, so there is no product to sell here. The provider "
            "itself may be worth a separate campaign."
        ),
        "research": {
            "company": {
                "name": "Meridian Broking",
                "what_they_do": "Retail equity and derivatives broking.",
                "engineering_org": "No in-house development function; roughly 12 technology staff in vendor management.",
                "delivery_signals": "Exchange filing names a single managed services provider for all platform development.",
            },
            "person": {"name": "Faisal Qureshi", "role": "VP Technology, effectively the vendor owner."},
            "signals": ["filing:outsourced-development"],
            "contact_channels": {"email": "verified", "linkedin": "active"},
            "inferred_pain_points": ["Not applicable — outside ICP"],
            "confidence": 0.88,
            "missing_fields": [],
        },
        "signals": [
            {
                "signal_type": "filing",
                "summary": "Exchange filing confirms all platform development is contracted to a single managed services provider under a five-year agreement running to 2029.",
                "source_url": "https://meridianbroking.in/investors/material-disclosures-2026",
                "days_ago": 58,
                "confidence": 0.91,
            },
        ],
        "follow_up_count": 0,
        "last_contacted_days": None,
        "next_action_hours": None,
        "current_channel": None,
        "channels_tried": [],
        "thread": None,
    },
    {
        "id": "psp_b_basu",
        "company": {
            "id": "co_upasana",
            "name": "Upasana Microfinance",
            "domain": "upasanamfi.in",
            "industry": "Microfinance institution",
            "headcount": 3900,
            "hq_location": "Kolkata, West Bengal",
            "funding_stage": "Listed",
            "enrichment": {
                "in_house_engineers": 165,
                "field_app": "In-house Android field collection app, 11,000 daily users",
                "regulator": "Reserve Bank of India",
            },
        },
        "full_name": "Ritwik Basu",
        "designation": "Head of Technology",
        "seniority": "Head",
        "email": "ritwik.basu@upasanamfi.in",
        "phone": None,
        "linkedin_url": "https://www.linkedin.com/in/ritwikbasu",
        "persona_match": "MFI technology head with a large in-house mobile estate",
        "source": "Investor presentation",
        "stage": "meeting",
        "status": "meeting_booked",
        "fit_score": 81.0,
        "fit_verdict": "qualified",
        "fit_criteria": _fit(90, 80, 88, 100, 72),
        "fit_rationale": (
            "Qualified and already converted. Upasana has 3,900 employees and an in-house "
            "engineering team of 165 maintaining a field collection app used by 11,000 field "
            "officers daily, so a regression is an operational event rather than an "
            "inconvenience. Basu sits below CXO but is the budget holder for technology tooling "
            "according to the investor presentation. A meeting was booked before this campaign "
            "was paused; the meeting itself is unaffected by the pause."
        ),
        "research": {
            "company": {
                "name": "Upasana Microfinance",
                "what_they_do": "Group lending to 2.8 million borrowers across eastern India.",
                "engineering_org": "165 engineers in Kolkata, Android and Java.",
                "delivery_signals": "Investor deck cites app stability as a field productivity driver.",
            },
            "person": {
                "name": "Ritwik Basu",
                "role": "Head of Technology, owns the tooling budget.",
                "public_writing": "Quoted in the FY26 investor presentation.",
                "likely_priorities": "Field app stability, release cadence, small team leverage.",
            },
            "signals": ["investor_deck:app-stability", "hiring:android-engineers"],
            "contact_channels": {"email": "verified", "linkedin": "active"},
            "inferred_pain_points": [
                "A field app regression stops collections for 11,000 officers the same day",
                "165 engineers supporting a mission-critical app with no automated review",
            ],
            "confidence": 0.84,
            "missing_fields": ["direct_phone"],
        },
        "signals": [
            {
                "signal_type": "investor_deck",
                "summary": "Upasana's FY26 investor presentation names field app stability as one of three drivers of collection efficiency, with a slide on release quality.",
                "source_url": "https://upasanamfi.in/investors/fy26-investor-presentation.pdf",
                "days_ago": 62,
                "confidence": 0.86,
            },
            {
                "signal_type": "hiring",
                "summary": "Recruiting eight Android engineers in Kolkata specifically for the field collection application.",
                "source_url": "https://upasanamfi.in/careers",
                "days_ago": 40,
                "confidence": 0.79,
            },
        ],
        "follow_up_count": 1,
        "last_contacted_days": 7,
        "next_action_days": 5,
        "current_channel": "email",
        "channels_tried": ["email", "linkedin"],
        "thread": {
            "channel": "email",
            "status": "meeting_booked",
            "sentiment": "positive",
            "messages": [
                {
                    "direction": "outbound",
                    "message_type": "initial_outreach",
                    "subject": "App stability as a collection efficiency driver",
                    "body": (
                        "Ritwik — your FY26 investor presentation is unusually direct about field app "
                        "stability being a collection efficiency driver rather than an IT metric. With "
                        "11,000 officers depending on it daily, a regression is a revenue event the "
                        "same afternoon.\n\n"
                        "Flightdeck reviews every pull request against the whole repository, which is "
                        "how it catches the change that breaks an offline sync path three modules "
                        "away. It runs inside your own ap-south-1 account.\n\n"
                        "Would the Android leads find a working session useful, or is release quality "
                        "already where you want it?"
                    ),
                    "status": "replied",
                    "days_ago": 21,
                    "confidence": 0.85,
                    "evidence_signal": 0,
                },
                {
                    "direction": "inbound",
                    "message_type": "reply",
                    "subject": "Re: App stability as a collection efficiency driver",
                    "body": (
                        "It is not where I want it. We had two offline sync regressions this year and "
                        "both were caught by field officers, not by us.\n\n"
                        "Send me a slot next week. Keep it to the Android team's world — I do not need "
                        "the analytics part yet."
                    ),
                    "status": "delivered",
                    "days_ago": 15,
                },
                {
                    "direction": "outbound",
                    "message_type": "follow_up",
                    "subject": "Re: App stability as a collection efficiency driver",
                    "body": (
                        "Thursday 4pm IST is held, invite sent. Agenda is one item: we point Flightdeck "
                        "at a copy of the field app repository and walk through the last two sync "
                        "regressions to see whether the review would have caught them.\n\n"
                        "No slides. If it would not have caught them, that is worth knowing too."
                    ),
                    "status": "delivered",
                    "days_ago": 7,
                    "confidence": 0.9,
                    "evidence_signal": 0,
                },
            ],
        },
    },
]


PROSPECTS_C: list[dict] = [
    {
        "id": "psp_c_marchetti",
        "company": {
            "id": "co_larkspur",
            "name": "Larkspur AI",
            "domain": "larkspur.ai",
            "industry": "Voice AI for healthcare",
            "headcount": 38,
            "hq_location": "San Francisco, CA",
            "funding_stage": "Series A",
            "enrichment": {
                "engineers": 24,
                "last_round": {"amount_usd": "21M", "date": "2026-08-04", "lead": "Fieldstone Ventures"},
                "stack": ["Python", "Rust", "Modal", "GitHub"],
                "agent_usage": "Public commit history shows agent-authored PRs",
            },
        },
        "full_name": "Elena Marchetti",
        "designation": "Co-founder & CTO",
        "seniority": "Founder",
        "email": "elena@larkspur.ai",
        "phone": "+1-415-555-0164",
        "linkedin_url": "https://www.linkedin.com/in/elenamarchetti",
        "persona_match": "Technical co-founder scaling from 24 to 45 engineers",
        "source": "Funding announcement + GitHub",
        "stage": "meeting",
        "status": "meeting_booked",
        "fit_score": 88.0,
        "fit_verdict": "qualified",
        "fit_criteria": _fit(95, 90, 85, 100, 86),
        "fit_rationale": (
            "Exactly who this campaign was built for. Series A voice AI company, 24 engineers "
            "growing to roughly 45, technical co-founder as the contact, and a funding event "
            "six weeks old so the budget conversation is live rather than theoretical. The "
            "strongest part is the evidence: their public repository shows agent-authored pull "
            "requests merging without a provenance trail, which is the Guard conversation "
            "handed to us."
        ),
        "research": {
            "company": {
                "name": "Larkspur AI",
                "what_they_do": "Voice agents for clinical intake and patient follow-up.",
                "engineering_org": "24 engineers, heavy agent usage, GitHub public and private repos.",
                "delivery_signals": "Agent-authored commits visible in the open-source SDK repository.",
            },
            "person": {
                "name": "Elena Marchetti",
                "role": "Co-founder and CTO, previously a speech researcher.",
                "public_writing": "Posts on the trade-offs of letting agents write production code.",
                "likely_priorities": "Shipping speed, not losing control of the codebase as the team doubles.",
            },
            "signals": ["funding:series-a", "open_source:agent-commits", "founder_post:agent-review"],
            "contact_channels": {"email": "verified", "linkedin": "active", "phone": "direct dial"},
            "inferred_pain_points": [
                "Agents are already writing production code with no provenance record",
                "Team doubling means the informal review culture stops working",
                "HIPAA scope arriving with the first hospital contract",
            ],
            "confidence": 0.87,
            "missing_fields": [],
        },
        "signals": [
            {
                "signal_type": "founder_post",
                "summary": "Marchetti posted that roughly a third of Larkspur's merged pull requests are now agent-authored and that reviewing them is the team's biggest time sink.",
                "source_url": "https://www.linkedin.com/posts/elenamarchetti-agent-authored-prs",
                "days_ago": 12,
                "confidence": 0.91,
            },
            {
                "signal_type": "funding",
                "summary": "Raised a $21M Series A led by Fieldstone Ventures, with the announcement naming a move from 24 to 45 engineers by mid-2027.",
                "source_url": "https://larkspur.ai/blog/series-a",
                "days_ago": 47,
                "confidence": 0.94,
            },
        ],
        "follow_up_count": 1,
        "last_contacted_days": 5,
        "next_action_days": 1,
        "current_channel": "email",
        "channels_tried": ["email", "voice"],
        "thread": {
            "channel": "email",
            "status": "meeting_booked",
            "sentiment": "positive",
            "messages": [
                {
                    "direction": "outbound",
                    "message_type": "initial_outreach",
                    "subject": "A third of your PRs are agent-authored",
                    "body": (
                        "Elena — your post about a third of Larkspur's merged pull requests being "
                        "agent-authored, and reviewing them being the biggest time sink, is the most "
                        "honest description of that problem I have seen from a founder.\n\n"
                        "Flightdeck reviews with a model that has indexed the whole repository, and "
                        "Guard records which model wrote what and on what basis, so the provenance "
                        "exists when a hospital security team asks. Twenty-four engineers is exactly "
                        "when this is cheap to put in.\n\n"
                        "Is the review load slowing merges yet, or just annoying?"
                    ),
                    "status": "replied",
                    "days_ago": 10,
                    "confidence": 0.89,
                    "evidence_signal": 0,
                },
                {
                    "direction": "inbound",
                    "message_type": "reply",
                    "subject": "Re: A third of your PRs are agent-authored",
                    "body": (
                        "Slowing merges. Two of us end up reviewing everything and we are the two "
                        "people who should be doing other things.\n\n"
                        "The provenance angle is more interesting than the review angle honestly — we "
                        "are mid-way through a hospital security review right now and 'an agent wrote "
                        "it' is not an answer they accept. Can you show that part specifically? "
                        "Wednesday or Thursday afternoon Pacific."
                    ),
                    "status": "delivered",
                    "days_ago": 7,
                },
                {
                    "direction": "outbound",
                    "message_type": "follow_up",
                    "subject": "Re: A third of your PRs are agent-authored",
                    "body": (
                        "Thursday 2pm Pacific, invite sent, and I will lead with Guard rather than "
                        "Review since that is the live problem.\n\n"
                        "What we will show: for any merged commit, which model or human authored it, "
                        "what context it was given, what policy checks ran and who approved the "
                        "result — exported as something a hospital security reviewer can read without "
                        "a demo. Bring whoever is handling that review."
                    ),
                    "status": "delivered",
                    "days_ago": 5,
                    "confidence": 0.92,
                    "evidence_signal": 0,
                },
            ],
        },
    },
    {
        "id": "psp_c_weber",
        "company": {
            "id": "co_echolane",
            "name": "Echolane",
            "domain": "echolane.ai",
            "industry": "Voice infrastructure",
            "headcount": 14,
            "hq_location": "Berlin, Germany",
            "funding_stage": "Seed",
            "enrichment": {
                "engineers": 11,
                "last_round": {"amount_eur": "4.2M", "date": "2026-07-22", "lead": "Kiefer Capital"},
                "stack": ["Rust", "Python", "GitHub"],
            },
        },
        "full_name": "Jonas Weber",
        "designation": "Founder & CEO",
        "seniority": "Founder",
        "email": "jonas@echolane.ai",
        "phone": None,
        "linkedin_url": "https://www.linkedin.com/in/jonasweber-echolane",
        "persona_match": "Seed founder, price-sensitive, technical",
        "source": "Funding announcement",
        "stage": "engaged",
        "status": "escalated",
        "fit_score": 71.0,
        "fit_verdict": "qualified",
        "fit_criteria": _fit(95, 90, 55, 100, 62),
        "fit_rationale": (
            "Qualified, with size as the obvious weakness. Eleven engineers is at the small end "
            "of what this campaign targets, so per-seat revenue is modest and the buyer will be "
            "price-sensitive by definition. Against that, Weber is the founder and sole decision "
            "maker, the seed round is eight weeks old, and Berlin sits inside our EU geography. "
            "Worth the touch; expect a pricing conversation immediately, which is what happened."
        ),
        "research": {
            "company": {
                "name": "Echolane",
                "what_they_do": "Low-latency speech-to-speech infrastructure for developers.",
                "engineering_org": "11 engineers, Rust-heavy, everything in one repository.",
                "delivery_signals": "Shipped a public developer preview in August.",
            },
            "person": {
                "name": "Jonas Weber",
                "role": "Founder and CEO, writes code daily.",
                "public_writing": "Changelog posts on the Echolane blog.",
                "likely_priorities": "Runway, latency benchmarks, not hiring before Series A.",
            },
            "signals": ["funding:seed", "product_launch:developer-preview"],
            "contact_channels": {"email": "verified", "linkedin": "active"},
            "inferred_pain_points": [
                "Eleven engineers carrying a public API with no dedicated review capacity",
                "Every euro of tooling spend competes with runway",
            ],
            "confidence": 0.73,
            "missing_fields": ["direct_phone"],
        },
        "signals": [
            {
                "signal_type": "product_launch",
                "summary": "Shipped a public developer preview of their speech-to-speech API with a published 310ms round-trip latency target.",
                "source_url": "https://echolane.ai/blog/developer-preview",
                "days_ago": 33,
                "confidence": 0.88,
            },
            {
                "signal_type": "funding",
                "summary": "Closed a €4.2M seed round led by Kiefer Capital in July, with hiring explicitly deferred until the preview converts.",
                "source_url": "https://echolane.ai/blog/seed-round",
                "days_ago": 60,
                "confidence": 0.9,
            },
        ],
        "follow_up_count": 2,
        "last_contacted_days": 4,
        "next_action_days": None,
        "current_channel": "email",
        "channels_tried": ["email"],
        "thread": {
            "channel": "email",
            "status": "escalated",
            "sentiment": "neutral",
            "escalation_reason": "Founder pushed back on per-seat pricing for an 11-person team and asked for a startup rate. The conversation agent is paused on this campaign, so the reply is waiting on a human.",
            "messages": [
                {
                    "direction": "outbound",
                    "message_type": "initial_outreach",
                    "subject": "310ms, eleven engineers, one repo",
                    "body": (
                        "Jonas — holding a 310ms round-trip target on a public preview with eleven "
                        "engineers is the kind of constraint where one bad merge costs a week.\n\n"
                        "Flightdeck reviews every pull request against the whole repository, so the "
                        "regression that adds 40ms in a path nobody was looking at gets flagged before "
                        "it lands rather than after a customer notices. At your size it is a few "
                        "minutes to install and it reviews the same day.\n\n"
                        "Is latency regression something you catch in CI already, or by feel?"
                    ),
                    "status": "replied",
                    "days_ago": 11,
                    "confidence": 0.81,
                    "evidence_signal": 0,
                },
                {
                    "direction": "inbound",
                    "message_type": "reply",
                    "subject": "Re: 310ms, eleven engineers, one repo",
                    "body": (
                        "By feel, mostly, which is not a good answer.\n\n"
                        "I looked at your pricing page. $39 per developer per month is $5,148 a year "
                        "for us and we are pre-revenue on the preview. That is real money against a "
                        "seed round. Is there a startup rate, or something usage-based? If it is list "
                        "price only then this is a conversation for after our Series A, not now."
                    ),
                    "status": "delivered",
                    "days_ago": 4,
                },
            ],
        },
    },
    {
        "id": "psp_c_duchamp",
        "company": {
            "id": "co_sonora",
            "name": "Sonora Labs",
            "domain": "sonoralabs.ai",
            "industry": "Speech recognition",
            "headcount": 92,
            "hq_location": "New York, NY",
            "funding_stage": "Series B",
            "enrichment": {
                "engineers": 61,
                "last_round": {"amount_usd": "45M", "date": "2026-05-12", "lead": "Harrow Lane Partners"},
                "stack": ["Python", "CUDA", "GitHub"],
            },
        },
        "full_name": "Alex Duchamp",
        "designation": "Chief Technology Officer",
        "seniority": "C-level",
        "email": "alex.duchamp@sonoralabs.ai",
        "phone": None,
        "linkedin_url": "https://www.linkedin.com/in/alexduchamp",
        "persona_match": "Series B CTO at the upper edge of the ICP",
        "source": "Conference talk + Apollo",
        "stage": "contacted",
        "status": "in_outreach",
        "fit_score": 76.0,
        "fit_verdict": "qualified",
        "fit_criteria": _fit(92, 88, 62, 100, 70),
        "fit_rationale": (
            "At the top edge of the band and still inside it. Sonora is Series B with 92 people "
            "and 61 engineers, against a ceiling of 120, so one more round takes them out of "
            "this campaign and into the US SaaS motion. Duchamp is CTO rather than founder, "
            "which costs a little on persona but removes the approval gate. The dated signal is "
            "a conference talk about their model release process, which is usable but not urgent."
        ),
        "research": {
            "company": {
                "name": "Sonora Labs",
                "what_they_do": "Domain-specific speech recognition models sold as an API.",
                "engineering_org": "61 engineers split between research and platform.",
                "delivery_signals": "Public talk on shipping model updates weekly without regressions.",
            },
            "person": {
                "name": "Alex Duchamp",
                "role": "CTO, joined 2025 from a large speech platform.",
                "public_writing": "Spoke at InterSpeech Industry Day 2026.",
                "likely_priorities": "Release safety for weekly model updates, research-to-platform handoff.",
            },
            "signals": ["conference_talk:weekly-model-releases", "funding:series-b"],
            "contact_channels": {"email": "verified", "linkedin": "active"},
            "inferred_pain_points": [
                "Research code reaching production with different review standards than platform code",
                "Weekly model releases mean the regression window is always open",
            ],
            "confidence": 0.75,
            "missing_fields": ["direct_phone", "current_tooling"],
        },
        "signals": [
            {
                "signal_type": "conference_talk",
                "summary": "Duchamp's InterSpeech Industry Day talk described shipping model updates weekly and named research-to-production handoff as the riskiest step.",
                "source_url": "https://interspeech-industry.example.com/2026/sonora-weekly-releases",
                "days_ago": 26,
                "confidence": 0.82,
            },
        ],
        "follow_up_count": 1,
        "last_contacted_days": 3,
        "next_action_days": 2,
        "current_channel": "email",
        "channels_tried": ["email"],
        "thread": {
            "channel": "email",
            "status": "awaiting_reply",
            "sentiment": "neutral",
            "messages": [
                {
                    "direction": "outbound",
                    "message_type": "initial_outreach",
                    "subject": "Research to production, weekly",
                    "body": (
                        "Alex — the part of your InterSpeech talk that stuck was calling the "
                        "research-to-production handoff the riskiest step in a weekly release cadence. "
                        "Research code tends to arrive with different review standards than platform "
                        "code, and the gap only shows up in production.\n\n"
                        "Flightdeck applies one review standard across both, with the whole repository "
                        "indexed, so the reviewer sees what a research change does to the serving path.\n\n"
                        "Is the handoff still weekly, or have you moved to a train?"
                    ),
                    "status": "delivered",
                    "days_ago": 8,
                    "confidence": 0.78,
                    "evidence_signal": 0,
                },
                {
                    "direction": "outbound",
                    "message_type": "follow_up",
                    "subject": "Re: Research to production, weekly",
                    "body": (
                        "One follow-up and then I will stop.\n\n"
                        "The thing worth testing is not whether a review tool finds bugs in research "
                        "code — it will, and most of them will be noise. It is whether it can tell "
                        "you that a change to a feature extractor alters behaviour for one customer's "
                        "acoustic profile. That is a whole-repository question.\n\n"
                        "Happy to run it against a repo of your choosing rather than talk about it."
                    ),
                    "status": "sent",
                    "days_ago": 3,
                    "confidence": 0.72,
                    "evidence_signal": 0,
                },
            ],
        },
    },
    {
        "id": "psp_c_lindgren",
        "company": {
            "id": "co_halden",
            "name": "Halden Voice",
            "domain": "halden.ai",
            "industry": "Voice AI for field operations",
            "headcount": 9,
            "hq_location": "Stockholm, Sweden",
            "funding_stage": "Seed",
            "enrichment": {"engineers": 7, "stack": ["TypeScript", "Python"], "accelerator": "Norrsken cohort 2026"},
        },
        "full_name": "Sofia Lindgren",
        "designation": "Founder & CEO",
        "seniority": "Founder",
        "email": "sofia@halden.ai",
        "phone": None,
        "linkedin_url": "https://www.linkedin.com/in/sofialindgren-halden",
        "persona_match": "Seed founder, EU, very early",
        "source": "Accelerator cohort listing",
        "stage": "qualified",
        "status": "awaiting_approval",
        "fit_score": 69.0,
        "fit_verdict": "qualified",
        "fit_criteria": _fit(92, 95, 38, 100, 60),
        "fit_rationale": (
            "Scrapes past the 68 threshold on persona strength rather than commercial size. "
            "Seven engineers is very small even for this campaign, so company size scores badly "
            "and the deal would be under €4,000 a year at list. What carries it is that Lindgren "
            "is the founder, decides alone, and has publicly described running coding agents "
            "across a seven-person team. Held at the founder seniority gate, as policy requires."
        ),
        "research": {
            "company": {
                "name": "Halden Voice",
                "what_they_do": "Hands-free voice reporting for field maintenance crews.",
                "engineering_org": "7 engineers, TypeScript and Python.",
                "delivery_signals": "Founder posts about agent-assisted development on a small team.",
            },
            "person": {
                "name": "Sofia Lindgren",
                "role": "Founder and CEO, technical.",
                "public_writing": "Regular posts on building with coding agents at seed stage.",
                "likely_priorities": "Leverage per engineer, getting to a Series A metric.",
            },
            "signals": ["founder_post:agent-leverage", "accelerator:norrsken"],
            "contact_channels": {"email": "verified", "linkedin": "active"},
            "inferred_pain_points": [
                "Seven engineers trying to ship like twenty using agents, with no review layer",
            ],
            "confidence": 0.68,
            "missing_fields": ["funding_amount", "direct_phone"],
        },
        "signals": [
            {
                "signal_type": "founder_post",
                "summary": "Lindgren wrote that Halden's seven engineers merge more agent-written code than human-written code and that nobody reviews the agent output properly.",
                "source_url": "https://www.linkedin.com/posts/sofialindgren-agent-written-majority",
                "days_ago": 9,
                "confidence": 0.84,
            },
        ],
        "follow_up_count": 0,
        "last_contacted_days": None,
        "next_action_hours": 10,
        "current_channel": None,
        "channels_tried": [],
        "thread": None,
    },
    {
        "id": "psp_c_blackwood",
        "company": {
            "id": "co_parley",
            "name": "Parley Systems",
            "domain": "parley.systems",
            "industry": "Conversational AI",
            "headcount": 46,
            "hq_location": "Austin, TX",
            "funding_stage": "Series A",
            "enrichment": {"engineers": 29, "stack": ["Go", "Python", "GitLab"]},
        },
        "full_name": "Theo Blackwood",
        "designation": "Chief Technology Officer",
        "seniority": "C-level",
        "email": "theo@parley.systems",
        "phone": None,
        "linkedin_url": "https://www.linkedin.com/in/theoblackwood",
        "persona_match": "Series A CTO, GitLab shop",
        "source": "Apollo + GitLab public group",
        "stage": "researched",
        "status": "researching",
        "fit_score": None,
        "fit_verdict": None,
        "fit_criteria": {},
        "fit_rationale": None,
        "research": {
            "company": {
                "name": "Parley Systems",
                "what_they_do": "Conversational AI for contact centre deflection.",
                "engineering_org": "29 engineers on self-managed GitLab.",
                "delivery_signals": "Public GitLab group shows a large open merge request backlog.",
            },
            "person": {
                "name": "Theo Blackwood",
                "role": "CTO and co-founder.",
                "public_writing": "None recent.",
                "likely_priorities": "Merge request throughput, moving off self-managed infrastructure.",
            },
            "signals": ["open_source:mr-backlog"],
            "contact_channels": {"email": "pattern-matched, unverified", "linkedin": "active"},
            "inferred_pain_points": ["Merge request backlog visible publicly; no dedicated review capacity"],
            "confidence": 0.58,
            "missing_fields": ["verified_email", "funding_date", "team_growth_plan"],
        },
        "signals": [
            {
                "signal_type": "open_source",
                "summary": "Parley's public GitLab group shows 63 open merge requests, 21 of them untouched for more than two weeks.",
                "source_url": "https://gitlab.com/parley-systems/-/merge_requests",
                "days_ago": 6,
                "confidence": 0.77,
            },
        ],
        "follow_up_count": 0,
        "last_contacted_days": None,
        "next_action_hours": 14,
        "current_channel": None,
        "channels_tried": [],
        "thread": None,
    },
    {
        "id": "psp_c_clarke",
        "company": {
            "id": "co_cadence_speech",
            "name": "Cadence Speech",
            "domain": "cadencespeech.ai",
            "industry": "Speech synthesis",
            "headcount": 31,
            "hq_location": "London, United Kingdom",
            "funding_stage": "Series A",
            "enrichment": {"engineers": 20},
        },
        "full_name": "Imogen Clarke",
        "designation": "Co-founder & CEO",
        "seniority": "Founder",
        "email": None,
        "phone": None,
        "linkedin_url": "https://www.linkedin.com/in/imogenclarke-cadence",
        "persona_match": "EU founder, Series A speech synthesis",
        "source": "Prospect Generation Agent, overnight batch",
        "stage": "discovered",
        "status": "pending",
        "fit_score": None,
        "fit_verdict": None,
        "fit_criteria": {},
        "fit_rationale": None,
        "research": {},
        "signals": [],
        "follow_up_count": 0,
        "last_contacted_days": None,
        "next_action_hours": 4,
        "current_channel": None,
        "channels_tried": [],
        "thread": None,
    },
    {
        "id": "psp_c_walsh",
        "company": {
            "id": "co_riverbend",
            "name": "Riverbend AI",
            "domain": "riverbend.ai",
            "industry": "Voice analytics",
            "headcount": 11,
            "hq_location": "Dublin, Ireland",
            "funding_stage": "Seed",
            "enrichment": {"engineers": 8, "stack": ["Python", "GitHub"]},
        },
        "full_name": "Conor Walsh",
        "designation": "Founder",
        "seniority": "Founder",
        "email": "conor.walsh@riverbend.ai",
        "phone": None,
        "linkedin_url": "https://www.linkedin.com/in/conorwalsh-riverbend",
        "persona_match": "Seed founder, opted out",
        "source": "Apollo",
        "stage": "contacted",
        "status": "closed",
        "fit_score": 66.0,
        "fit_verdict": "borderline",
        "fit_criteria": _fit(88, 95, 40, 100, 52),
        "fit_rationale": (
            "Borderline on size at eight engineers and thin on signal, but the founder persona "
            "and EU geography carried it over the line for a single touch. Moot now: Walsh "
            "asked to be removed on the first reply, the conversation was closed immediately "
            "and his address was added to the global suppression list. No follow-up will be "
            "attempted by this or any other campaign."
        ),
        "research": {
            "company": {
                "name": "Riverbend AI",
                "what_they_do": "Conversation analytics for sales teams.",
                "engineering_org": "8 engineers in Dublin.",
                "delivery_signals": "Limited public engineering footprint.",
            },
            "person": {"name": "Conor Walsh", "role": "Founder, non-technical background."},
            "signals": ["product_launch:beta"],
            "contact_channels": {"email": "verified", "linkedin": "active"},
            "inferred_pain_points": ["Insufficient evidence"],
            "confidence": 0.54,
            "missing_fields": ["engineering_leadership", "review_process"],
        },
        "signals": [
            {
                "signal_type": "product_launch",
                "summary": "Opened a private beta of their conversation analytics product to design partners in August.",
                "source_url": "https://riverbend.ai/blog/private-beta",
                "days_ago": 41,
                "confidence": 0.69,
            },
        ],
        "follow_up_count": 0,
        "last_contacted_days": 8,
        "next_action_hours": None,
        "current_channel": "email",
        "channels_tried": ["email"],
        "thread": {
            "channel": "email",
            "status": "closed",
            "sentiment": "negative",
            "escalation_reason": None,
            "messages": [
                {
                    "direction": "outbound",
                    "message_type": "initial_outreach",
                    "subject": "Design partners and eight engineers",
                    "body": (
                        "Conor — congratulations on opening the private beta to design partners last "
                        "month. That is usually the point at which eight engineers discover they are "
                        "supporting production for the first time.\n\n"
                        "Flightdeck reviews every pull request with the whole repository in context, "
                        "which at your size mostly means your two most senior people stop being the "
                        "review queue.\n\n"
                        "Is that already a constraint, or still comfortable?"
                    ),
                    "status": "replied",
                    "days_ago": 8,
                    "confidence": 0.66,
                    "evidence_signal": 0,
                },
                {
                    "direction": "inbound",
                    "message_type": "reply",
                    "subject": "Re: Design partners and eight engineers",
                    "body": (
                        "Please remove me from your list and do not contact me again, here or on "
                        "LinkedIn. We are not buying developer tooling and I do not want follow-ups.\n\n"
                        "Unsubscribe."
                    ),
                    "status": "delivered",
                    "days_ago": 7,
                },
            ],
        },
    },
    {
        "id": "psp_c_farrow",
        "company": {
            "id": "co_northgate_speech",
            "name": "Northgate Speech Systems",
            "domain": "northgatespeech.com",
            "industry": "Enterprise speech platforms",
            "headcount": 210,
            "hq_location": "Munich, Germany",
            "funding_stage": "Series C",
            "enrichment": {"engineers": 96},
        },
        "full_name": "Dominic Farrow",
        "designation": "VP Sales",
        "seniority": "VP",
        "email": "d.farrow@northgatespeech.com",
        "phone": None,
        "linkedin_url": "https://www.linkedin.com/in/dominicfarrow",
        "persona_match": "Outside ICP on stage, size and persona",
        "source": "Apollo",
        "stage": "researched",
        "status": "rejected",
        "fit_score": 24.0,
        "fit_verdict": "rejected",
        "fit_criteria": _fit(80, 10, 15, 100, 30),
        "fit_rationale": (
            "Rejected on three counts, any one of which would be sufficient. Northgate is Series "
            "C with 210 employees, past both the funding ceiling and the 120-person headcount "
            "limit for this campaign. More decisively, Farrow owns revenue, not engineering: "
            "this campaign's entire argument is a technical one made to a technical founder, and "
            "there is no version of it that lands with a VP of Sales. Do not re-add on a "
            "geography widen."
        ),
        "research": {
            "company": {
                "name": "Northgate Speech Systems",
                "what_they_do": "Enterprise speech platforms for automotive and industrial customers.",
                "engineering_org": "96 engineers, Munich and Prague.",
                "delivery_signals": "Not assessed — excluded before enrichment completed.",
            },
            "person": {"name": "Dominic Farrow", "role": "VP Sales, EMEA."},
            "signals": [],
            "contact_channels": {"email": "verified", "linkedin": "active"},
            "inferred_pain_points": ["Not applicable — outside ICP"],
            "confidence": 0.8,
            "missing_fields": [],
        },
        "signals": [
            {
                "signal_type": "funding",
                "summary": "Closed a Series C in March 2026, one stage beyond this campaign's Series B ceiling.",
                "source_url": "https://northgatespeech.com/press/series-c",
                "days_ago": 88,
                "confidence": 0.83,
            },
        ],
        "follow_up_count": 0,
        "last_contacted_days": None,
        "next_action_hours": None,
        "current_channel": None,
        "channels_tried": [],
        "thread": None,
    },
]

PROSPECTS_BY_CAMPAIGN: dict[str, list[dict]] = {
    CMP_A: PROSPECTS_A,
    CMP_B: PROSPECTS_B,
    CMP_C: PROSPECTS_C,
    CMP_D: [],
}


# ---------------------------------------------------------------------------
# Approvals. Seven pending, which is the number the dashboard summary shows.
# Every one is tied to a real prospect, and the two that gate a specific piece
# of text are tied to the message a human has to read before it can send.
# ---------------------------------------------------------------------------
APPROVALS: list[dict] = [
    {
        "campaign_id": CMP_C,
        "prospect_id": "psp_c_weber",
        "attach_to": "last_inbound",
        "title": "Voice SDR Agent flagged a pricing objection",
        "reason": "Needs human reply",
        "detail": (
            "Jonas Weber (Founder & CEO, Echolane) has asked for a startup rate or usage-based "
            "pricing for 11 developers and has said that list price would push this past their "
            "Series A. Campaign policy escalates every pricing negotiation to a human, and the "
            "conversation agent is paused on this campaign, so nothing will be sent until "
            "someone answers. Suggested position: Team tier at 11 seats is $5,148 a year; we "
            "have discretion to offer 12 months at the seed rate with a step-up on their next "
            "round. That discount is not the agent's to give."
        ),
        "hours_ago": 2,
    },
    {
        "campaign_id": CMP_A,
        "prospect_id": "psp_a_lindqvist",
        "attach_to": None,
        "title": "Outreach Strategy Agent wants to contact a VP-level exec",
        "reason": "Above seniority threshold",
        "detail": (
            "Rebecca Lindqvist is VP Platform Engineering at Tidemark Health and scores 80 "
            "against this campaign's ICP. She sits above the seniority threshold configured for "
            "US SaaS · CTO Outreach, so the first touch needs a human to approve both the "
            "approach and the sending identity. The proposed angle is the newly created platform "
            "function and the two Developer Experience roles posted under it. Tidemark is "
            "HIPAA and HITRUST scoped, so expect a security review before any trial."
        ),
        "hours_ago": 14,
    },
    {
        "campaign_id": CMP_A,
        "prospect_id": "psp_a_sousa",
        "attach_to": "pending_draft",
        "title": "Personalisation Agent's draft scored low confidence",
        "reason": "Low confidence",
        "detail": (
            "The follow-up drafted for Ana Sousa (VP Engineering, Cobalt Yard) scored 0.51 "
            "against a threshold of 0.62 on this campaign. The cause is thin evidence: the only "
            "dated signal we hold for Cobalt Yard is a conference talk from six weeks ago about "
            "tool consolidation, and the draft leans on a deployment claim that is true of the "
            "product but not evidenced for their environment. Approve, edit, or send the "
            "research agent back for a stronger signal before this goes out."
        ),
        "hours_ago": 26,
    },
    {
        "campaign_id": CMP_B,
        "prospect_id": "psp_b_raghavan",
        "attach_to": None,
        "title": "A prospect here is also in US SaaS · CTO Outreach",
        "reason": "Conflict",
        "detail": (
            "Nikhil Raghavan (nikhil.raghavan@silverlinefin.com) exists in both India BFSI · CIO "
            "Outreach and US SaaS · CTO Outreach under the same identity key. The US record made "
            "contact five days ago, so the duplicate-outreach guardrail has blocked this "
            "campaign from sending. Both records are legitimate: research resolved Silverline's "
            "US parent at 470 employees and its India entity at 1,240, and each matched a "
            "different ICP. A human needs to decide which campaign owns the relationship. "
            "Recommended: leave ownership with US SaaS · CTO Outreach and close this record, "
            "since the decision maker sits in New York."
        ),
        "hours_ago": 41,
    },
    {
        "campaign_id": CMP_C,
        "prospect_id": "psp_c_lindgren",
        "attach_to": None,
        "title": "Founder-level contact needs sign-off before first touch",
        "reason": "Above seniority threshold",
        "detail": (
            "Sofia Lindgren is Founder & CEO of Halden Voice, which puts her above the seniority "
            "threshold on this campaign. Fit score is 69 against a threshold of 68, carried "
            "almost entirely by persona: seven engineers makes this a sub-€4,000 deal at list. "
            "The signal is strong and recent — she posted nine days ago that Halden merges more "
            "agent-written code than human-written code and that nobody reviews it properly. "
            "Worth a touch as a design-partner conversation rather than a sale."
        ),
        "hours_ago": 55,
    },
    {
        "campaign_id": CMP_B,
        "prospect_id": "psp_b_krishnan",
        "attach_to": "last_inbound",
        "title": "Data residency and pricing commitment requested in writing",
        "reason": "Needs human reply",
        "detail": (
            "Deepa Krishnan (Head of Digital Technology, Sanchay Life Insurance) has asked for "
            "an indicative annual price for roughly 300 developers and a written confirmation "
            "that no code, prompt or metadata leaves Indian jurisdiction, including for model "
            "inference. Both are outside what any agent may answer on this campaign. The honest "
            "answer on the second question is that this requires the Enterprise tier with "
            "self-hosted inference in ap-south-1; the cloud tiers cannot make that commitment. "
            "Legal should supply the written wording."
        ),
        "hours_ago": 68,
    },
    {
        "campaign_id": CMP_A,
        "prospect_id": "psp_a_ramirez",
        "attach_to": None,
        "title": "ICP Fitment Agent returned a borderline score for Fernpost",
        "reason": "Borderline ICP fit",
        "detail": (
            "Joel Ramirez (CTO, Fernpost) scored 58, exactly on this campaign's borderline line "
            "and 17 points below the qualification threshold. Industry, seniority and geography "
            "are clean; size and evidence are not. Thirty-one engineers is six above our floor, "
            "and the only dated signal is a careers page refresh with no platform or developer "
            "experience roles in it. The agent's recommendation is to reject unless a stronger "
            "signal appears within 30 days. A human decision keeps the ICP honest either way."
        ),
        "hours_ago": 79,
    },
]


# ---------------------------------------------------------------------------
# Global do-not-contact list. Checked before every outbound action, across
# every campaign, including the domain-level entry.
# ---------------------------------------------------------------------------
SUPPRESSIONS: list[dict] = [
    {
        "email": "conor.walsh@riverbend.ai",
        "reason": "unsubscribed",
        "scope": "global",
        "days_ago": 7,
    },
    {
        "email": "j.fitzgerald@marlowetech.com",
        "reason": "bounced",
        "scope": "global",
        "days_ago": 23,
    },
    {
        "domain": "devsynth.ai",
        "reason": "competitor domain",
        "scope": "global",
        "days_ago": 96,
    },
    {
        "email": "compliance@anvayabank.in",
        "reason": "manual do-not-contact",
        "scope": "global",
        "days_ago": 12,
    },
    {
        "phone": "+1-415-555-0188",
        "reason": "manual do-not-contact",
        "scope": "global",
        "days_ago": 34,
    },
    {
        "linkedin_url": "https://www.linkedin.com/in/hbarros-platform",
        "reason": "asked not to be contacted on LinkedIn",
        "scope": "global",
        "days_ago": 51,
    },
]


# ---------------------------------------------------------------------------
# Activity feed. Every message is phrased as a continuation, because the
# frontend renders the campaign name in bold immediately before it.
# ---------------------------------------------------------------------------
AUDIT_EVENTS: list[dict] = [
    # -- Campaign B -------------------------------------------------------
    {"h": 3, "c": CMP_B, "et": "campaign", "eid": CMP_B, "ev": "campaign.paused", "sev": "info", "actor": "A. Iyer",
     "msg": "paused by A. Iyer. All autonomous execution stopped."},
    {"h": 3.2, "c": CMP_B, "et": "campaign", "eid": CMP_B, "ev": "guardrail.blocked", "sev": "warning", "actor": "guardrails",
     "msg": "blocked outreach to Nikhil Raghavan: already in active outreach under US SaaS · CTO Outreach"},
    {"h": 68, "c": CMP_B, "et": "prospect", "eid": "psp_b_krishnan", "ev": "approval.requested", "sev": "warning", "actor": "conversation agent",
     "msg": "escalated a pricing and data residency question from Sanchay Life Insurance to a human"},
    {"h": 76, "c": CMP_B, "et": "prospect", "eid": "psp_b_basu", "ev": "meeting.booked", "sev": "success", "actor": "conversation agent",
     "msg": "booked a technical session with Upasana Microfinance for Thursday 4pm IST"},
    {"h": 96, "c": CMP_B, "et": "prospect", "eid": "psp_b_qureshi", "ev": "prospect.rejected", "sev": "info", "actor": "icp fitment agent",
     "msg": "rejected Meridian Broking: application development is fully outsourced, no in-house pull requests to review"},
    {"h": 120, "c": CMP_B, "et": "campaign", "eid": CMP_B, "ev": "agent.batch_completed", "sev": "info", "actor": "research enrichment agent",
     "msg": "enriched 6 prospects, 2 held for a human because fewer than two dated signals were found"},
    {"h": 141, "c": CMP_B, "et": "message", "eid": "psp_b_venkatesan", "ev": "message.sent", "sev": "success", "actor": "A. Iyer",
     "msg": "sent a second touch to the CIO of Anvaya Bank, inside working hours as policy requires"},
    {"h": 160, "c": CMP_B, "et": "campaign", "eid": CMP_B, "ev": "rate_limit.reached", "sev": "warning", "actor": "guardrails",
     "msg": "hit its daily send limit of 18 with 4 prospects still queued, deferred to tomorrow"},

    # -- Campaign A -------------------------------------------------------
    {"h": 2, "c": CMP_A, "et": "prospect", "eid": "psp_a_sousa", "ev": "approval.requested", "sev": "warning", "actor": "personalisation agent",
     "msg": "held a follow-up draft for Cobalt Yard at confidence 0.51, below the 0.62 threshold"},
    {"h": 4.5, "c": CMP_A, "et": "prompt_version", "eid": "prompt", "ev": "prompt.activated", "sev": "success", "actor": "D. Fernandes",
     "msg": "prompt v3 activated for the Personalisation Agent"},
    {"h": 14, "c": CMP_A, "et": "prospect", "eid": "psp_a_lindqvist", "ev": "approval.requested", "sev": "warning", "actor": "outreach strategy agent",
     "msg": "requested sign-off before contacting a VP-level exec at Tidemark Health"},
    {"h": 26, "c": CMP_A, "et": "prospect", "eid": "psp_a_ramirez", "ev": "approval.requested", "sev": "info", "actor": "icp fitment agent",
     "msg": "returned a borderline score of 58 for Fernpost and routed it to a human"},
    {"h": 31, "c": CMP_A, "et": "message", "eid": "psp_a_okafor", "ev": "meeting.booked", "sev": "success", "actor": "conversation agent",
     "msg": "booked a technical session with Brightpath Analytics for Friday 10am Central"},
    {"h": 52, "c": CMP_A, "et": "prospect", "eid": "psp_a_feld", "ev": "reply.received", "sev": "success", "actor": "conversation agent",
     "msg": "received a positive reply from Northwind Ledger asking about VPC deployment and training guarantees"},
    {"h": 73, "c": CMP_A, "et": "campaign", "eid": CMP_A, "ev": "agent.batch_completed", "sev": "info", "actor": "icp fitment agent",
     "msg": "qualified 14 of 22 prospects overnight, rejected 5 and routed 3 to a human"},
    {"h": 79, "c": CMP_A, "et": "prospect", "eid": "psp_a_nakamura", "ev": "prospect.rejected", "sev": "info", "actor": "icp fitment agent",
     "msg": "rejected Ledgerloop: 12 engineers and seed stage, below both the size floor and the funding minimum"},
    {"h": 98, "c": CMP_A, "et": "message", "eid": "psp_a_wexler", "ev": "message.sent", "sev": "success", "actor": "M. Rao",
     "msg": "sent the Azure reference architecture and SOC 2 report to Pinewell Retail Cloud ahead of Tuesday's session"},
    {"h": 112, "c": CMP_A, "et": "campaign", "eid": CMP_A, "ev": "conflict.detected", "sev": "warning", "actor": "guardrails",
     "msg": "3 prospects also appear in India BFSI · CIO Outreach, single-touch owner assigned"},
    {"h": 129, "c": CMP_A, "et": "prospect", "eid": "psp_a_raghavan", "ev": "message.sent", "sev": "success", "actor": "personalisation agent",
     "msg": "opened outreach to Silverline Fintech citing their ledger extraction write-up"},
    {"h": 148, "c": CMP_A, "et": "campaign", "eid": CMP_A, "ev": "agent.batch_completed", "sev": "info", "actor": "prospect generation agent",
     "msg": "added 31 new prospects from the overnight batch, 9 discarded against the exclusion list"},
    {"h": 166, "c": CMP_A, "et": "prompt_version", "eid": "prompt", "ev": "prompt.created", "sev": "info", "actor": "D. Fernandes",
     "msg": "prompt v3 drafted for the Personalisation Agent, banning funding-round references older than 6 months"},
    {"h": 178, "c": CMP_A, "et": "campaign", "eid": CMP_A, "ev": "channel.rate_limited", "sev": "warning", "actor": "guardrails",
     "msg": "LinkedIn touches deferred for 90 minutes after the connector reported a throttle"},
    {"h": 194, "c": CMP_A, "et": "prospect", "eid": "psp_a_venkataraman", "ev": "message.sent", "sev": "success", "actor": "personalisation agent",
     "msg": "sent a first touch to Quillstream referencing 18 open engineering roles against a team of 58"},

    # -- Campaign C -------------------------------------------------------
    {"h": 0.6, "c": CMP_C, "et": "campaign", "eid": CMP_C, "ev": "agent.batch_completed", "sev": "info", "actor": "prospect generation agent",
     "msg": "added 12 new founders from the EU widen, 4 discarded for being past Series B"},
    {"h": 2.4, "c": CMP_C, "et": "prospect", "eid": "psp_c_weber", "ev": "approval.requested", "sev": "warning", "actor": "conversation agent",
     "msg": "escalated a pricing objection from Echolane rather than answering it"},
    {"h": 9, "c": CMP_C, "et": "agent_config", "eid": "conversation", "ev": "agent.paused", "sev": "warning", "actor": "S. Menon",
     "msg": "conversation agent paused after it answered a pricing question unsupervised, replies now wait for a human"},
    {"h": 18, "c": CMP_C, "et": "campaign", "eid": CMP_C, "ev": "channel.paused", "sev": "warning", "actor": "S. Menon",
     "msg": "SMS paused after a message landed at 06:40 local time in Berlin, email and voice unaffected"},
    {"h": 55, "c": CMP_C, "et": "prospect", "eid": "psp_c_lindgren", "ev": "approval.requested", "sev": "info", "actor": "outreach strategy agent",
     "msg": "requested founder-level sign-off before the first touch to Halden Voice"},
    {"h": 82, "c": CMP_C, "et": "prospect", "eid": "psp_c_walsh", "ev": "suppression.added", "sev": "warning", "actor": "conversation agent",
     "msg": "closed the Riverbend AI thread on an opt-out request and added the address to the global suppression list"},
    {"h": 104, "c": CMP_C, "et": "prospect", "eid": "psp_c_marchetti", "ev": "meeting.booked", "sev": "success", "actor": "conversation agent",
     "msg": "booked a Guard-focused session with Larkspur AI for Thursday 2pm Pacific"},
    {"h": 130, "c": CMP_C, "et": "campaign", "eid": CMP_C, "ev": "provider.error", "sev": "error", "actor": "email channel",
     "msg": "email provider rate limit hit, retried automatically after 90 seconds and all 6 messages delivered"},
    {"h": 151, "c": CMP_C, "et": "prospect", "eid": "psp_c_farrow", "ev": "prospect.rejected", "sev": "info", "actor": "icp fitment agent",
     "msg": "rejected Northgate Speech Systems: Series C, 210 employees and a revenue-side contact"},
    {"h": 172, "c": CMP_C, "et": "prospect", "eid": "psp_c_duchamp", "ev": "message.sent", "sev": "success", "actor": "personalisation agent",
     "msg": "sent a follow-up to Sonora Labs on research-to-production handoff risk"},
    {"h": 190, "c": CMP_C, "et": "campaign", "eid": CMP_C, "ev": "voice.completed", "sev": "success", "actor": "voice channel",
     "msg": "completed 4 voice attempts, 1 connected and 3 went to voicemail with no message left"},

    # -- Campaign D and platform -----------------------------------------
    {"h": 25, "c": CMP_D, "et": "campaign", "eid": CMP_D, "ev": "campaign.blocked", "sev": "info", "actor": "guardrails",
     "msg": "is still a draft with no channels configured, so no outreach can be sent from it"},
    {"h": 25.5, "c": CMP_D, "et": "campaign", "eid": CMP_D, "ev": "campaign.updated", "sev": "info", "actor": "S. Menon",
     "msg": "expansion ICP updated to require Review adoption above 70% of active repositories"},
    {"h": 6, "c": None, "et": "platform", "eid": "kill_switch", "ev": "platform.checked", "sev": "info", "actor": "S. Menon",
     "msg": "global kill switch verified disengaged during the morning operations check"},
    {"h": 44, "c": None, "et": "user", "eid": USR_SHARMA, "ev": "user.deactivated", "sev": "warning", "actor": "S. Menon",
     "msg": "K. Sharma deactivated on offboarding; his sending identity was removed from every campaign"},
    {"h": 200, "c": None, "et": "knowledge_document", "eid": "knowledge", "ev": "knowledge.ingested", "sev": "success", "actor": "S. Menon",
     "msg": "security and compliance one-pager re-ingested after the ISO 27701 certificate was renewed"},
]


# ---------------------------------------------------------------------------
# Knowledge base. Ingested through the normal path so the chunks are embedded
# and retrievable, which is what personalisation actually reads at run time.
# ---------------------------------------------------------------------------
KB_PRODUCT_OVERVIEW = """
Corvus Flightdeck: product overview

Flightdeck is an AI engineering platform. It installs on top of an existing GitHub Enterprise,
GitLab or Bitbucket Data Center estate and does not ask a team to change how they work. There
are four modules and most customers start with one.

Flightdeck Review reads the whole repository, not just the diff. It builds and maintains an
index of every service, caller, configuration file and past review comment, so when a pull
request arrives the review is written with that context. In practice this is the difference
between a comment that says "consider extracting this function" and one that says "this change
alters the shape of the payload consumed by billing-core and invoice-worker; neither has been
updated." Teams that have tried diff-only review tools recognise the distinction immediately,
because the first kind of comment is what taught their seniors to ignore the tool.

Flightdeck Insights attributes delivery metrics to the individual pull request. Deployment
frequency, lead time for change, change failure rate and time to restore are computed per PR,
per service, per team and per author, which means an engineering leader can answer where the
time goes with evidence rather than with a survey. The most common use is defending a platform
team's roadmap in a budget conversation.

Flightdeck Agents runs supervised coding agents for the work engineers postpone: framework
migrations, test backfill, dependency upgrades and codemods across many repositories. Every
agent run opens an ordinary pull request that goes through the same review and the same
approvals as human work. Nothing merges itself.

Flightdeck Guard is the governance layer. It records provenance for every change: which model
or which human authored it, what context the model was given, which policy checks ran, and who
approved the result. It enforces policy before merge, so a team can allow coding agents in
regulated code paths without discovering after the fact that nobody can explain a line.

Deployment is cloud, customer VPC, or fully self-hosted with self-hosted inference. Source code
is never used to train models, and that commitment sits in the data processing agreement rather
than on a marketing page. Typical time from first repository connected to first reviewed pull
request is under an hour.
""".strip()

KB_PRICING = """
Pricing and packaging

Flightdeck is priced per active developer per month. An active developer is someone who opened,
reviewed or merged at least one pull request in the billing month; seats are not pre-purchased
and unused seats are not charged.

Team — $39 per developer per month, billed annually. Includes Flightdeck Review and Flightdeck
Insights. Up to 60 developers. Cloud deployment only, in the customer's choice of us-east-1,
eu-west-1 or ap-south-1. Self-serve, no implementation fee, 14-day trial without a card. This
is the tier most Seed and Series A companies buy, and at 11 to 25 engineers the annual number
lands between $5,000 and $12,000.

Scale — $69 per developer per month, billed annually. Adds Flightdeck Agents, SSO and SCIM,
private VPC deployment inside the customer's own cloud account, audit log export, and a 99.9%
uptime SLA. No developer cap. This is the common landing tier for Series B to Series D
companies between 50 and 300 engineers.

Enterprise — from $148,000 per year. Adds Flightdeck Guard, self-hosted inference for fully
air-gapped operation, in-region data residency with customer-managed encryption keys, a named
solutions architect, a custom data processing agreement, and security questionnaire support.
Required for any deployment where source code may not leave the customer's perimeter, which
includes most regulated financial institutions.

Discounting discipline. Reps may offer up to 15% for a two-year commitment and up to 20% for a
three-year commitment without approval. Seed-stage startups below 15 engineers may be offered
12 months at the Team rate with a contractual step-up on their next priced round; this is a
manager decision, not a rep decision, and never an agent decision. Usage-based pricing does not
exist and should not be implied.

What is never charged separately: repository count, review volume, API calls, storage, or
additional non-developer viewers such as engineering managers and product leads. Professional
services are quoted only for migrations from an incumbent tool with more than 500 repositories.
""".strip()

KB_SECURITY = """
Security, privacy and compliance one-pager

Certifications. SOC 2 Type II (current report available under NDA, renewed March 2026), ISO
27001, ISO 27701, and GDPR compliance with a standard data processing agreement and EU standard
contractual clauses. Our processing is aligned to India's Digital Personal Data Protection Act
2023 for customers hosted in ap-south-1. Penetration tests are run twice yearly by an
independent firm; the executive summary is shareable under NDA.

Data handling. Customer source code is processed, never retained for training. This is a
contractual commitment in the DPA, not a policy statement that can be changed unilaterally.
Repository indexes and embeddings are encrypted at rest with AES-256 and, on Enterprise, with
customer-managed keys held in the customer's own KMS. Deleting a repository connection purges
its index and embeddings within 24 hours, and the purge is logged.

Deployment models. Cloud multi-tenant, with regional isolation in us-east-1, eu-west-1 and
ap-south-1. Private VPC, where the control plane and the index run inside the customer's own
cloud account and only telemetry crosses the boundary. Self-hosted, where inference runs on
customer-operated GPUs and nothing whatsoever leaves the perimeter; this is the configuration
regulated financial institutions buy, and it is the only configuration for which we will sign a
statement that no code, prompt or metadata leaves a jurisdiction.

Access and identity. SAML SSO and SCIM provisioning on Scale and above. Role-based access
control maps to repository permissions in the source control system, so Flightdeck can never
show a user a file they could not already read. Break-glass access by Corvus staff requires
customer approval per incident, is time-boxed, and is written to the customer's audit log.

Auditability. Every action — index build, review, agent run, policy decision, approval — is
recorded with actor, timestamp, model identifier and the prompt version in force at the time.
Logs export to Splunk, Datadog or S3. For regulated customers this record is usually the reason
the platform clears review: it answers "why did the system do that" without a reconstruction.

Sub-processors. A current list is published and customers receive 30 days' notice of any
addition, with a right to object.
""".strip()

KB_COMPETITORS = """
Competitive positioning

Against diff-only AI review tools. The category that Flightdeck is most often compared to reads
the pull request diff and nothing else. That design is cheap to run and it produces two kinds of
output: correct observations about the lines in front of it, and confident nonsense about lines
it cannot see. Engineering teams learn within a month to skim past it, which is why so many
evaluations begin with "we tried two of these and both produced noise". The honest framing when
a prospect says this is to agree: they are describing the category accurately. The difference is
the index. Flightdeck maintains a repository-wide graph of callers, configuration and prior
review decisions, so it can say which three services a signature change breaks. Ask a prospect
to test exactly that and nothing else.

Against DORA and engineering analytics vendors. Analytics products report metrics from Git and
ticket metadata. They are usually accurate at team level and unconvincing at pull request level,
because they infer rather than observe. Flightdeck computes the same metrics from the review
pipeline it already runs, which is why attribution goes down to the individual change. The
competitive risk here is not capability, it is incumbency: if a team already pays for an
analytics tool, lead with Review and let Insights replace the incumbent at renewal.

Against coding agent vendors. Agent products optimise for code produced. Flightdeck optimises
for code that survives review, with a record of where it came from. When a prospect already uses
agents heavily — which is increasingly common at seed and Series A — the Guard conversation is
stronger than the Review conversation, because their problem is no longer writing code, it is
explaining it to a security reviewer.

Against building it internally. A platform team can build repository-aware review in about two
quarters and then owns it forever. This argument wins on opportunity cost, never on difficulty.

What we lose on. Teams under ten engineers rarely have enough review volume to justify the
spend. Organisations that have fully outsourced development have no pull requests to review.
Both are legitimate losses and should be disqualified early rather than pursued.
""".strip()

KB_A_ICP = """
ICP definition: US SaaS, Series B to Series D engineering leaders

Who qualifies. US-headquartered B2B software companies that have raised a Series B, C or D,
with total headcount between 50 and 500 and at least 25 engineers. The engineering floor matters
more than the headcount band: a 400-person company with 20 engineers is a worse fit than a
90-person company with 45. Vertical SaaS, developer tools and fintech infrastructure all count.

Who we sell to. The CTO, VP Engineering, VP Platform Engineering, SVP Engineering or Head of
Engineering. The buying authority for a tool that touches every repository sits at this level
and rarely below it. Directors of Engineering are champions, not buyers, and a campaign that
targets them produces meetings that do not convert.

Buying triggers, in descending order of usefulness. First, a public statement about review
latency, incident load or AI tooling — an engineering blog post, a conference talk, a status
page pattern. This is the strongest signal we can get because it is the prospect describing our
problem in their own words. Second, engineering headcount growth above 30% in two quarters,
which reliably breaks an informal review culture. Third, a first Head of Platform or Developer
Experience hire, because that person needs a baseline metric within 90 days. Fourth, a funding
round, but only within six months and only as context, never as the opening line.

Disqualifiers. Fewer than 25 engineers; pre-Series B or bootstrapped; agencies, consultancies
and staffing firms, which have no shared codebase to index; direct competitors in AI code
review. Companies above 500 employees are not rejected outright but should be routed to the
enterprise motion, because their procurement looks nothing like this campaign's assumptions.

Scoring guidance. Industry and geography are close to binary. Seniority is binary above VP.
Company size should be scored against engineering count, not total headcount. Signal strength
is the dimension that separates a 90 from a 75, and a prospect with no dated signal in the last
90 days should not score above borderline regardless of how well everything else fits.
""".strip()

KB_A_PLAYBOOK = """
Sales playbook: US SaaS CTO motion

The shape of this motion. One strong signal, one specific observation, one question. Engineering
leaders receive a great deal of outreach and almost all of it opens with a compliment or a
funding round. The fastest way to be read is to demonstrate that you have looked at something
they made — their engineering blog, their status page, their public repositories — and to say
something about it that is true and slightly uncomfortable.

First touch. Email, always, on this campaign. Sixty to a hundred and ten words. Open with the
observation, not with who we are. State the mechanism in one sentence, not the benefit: "reads
the whole repository rather than the diff" lands where "improves developer productivity" does
not. Close with a question about their situation. Do not ask for thirty minutes in a first
touch; a question gets answered, a calendar request gets archived.

Second touch, 72 hours later. Add one new piece of substance, never a reminder. The best second
touches suggest a number they could go and measure themselves — median time from pull request
opened to first substantive review comment is the one that works most often, because it is easy
to get and usually worse than they expect.

Third touch, channel switch to LinkedIn. Under ninety words, no subject, no repetition of the
email. If there is no reply after the third touch, close the record. Chasing costs more brand
than the deal is worth.

Handling the sceptic. Most engineering leaders worth selling to have tried a diff-only review
tool and been disappointed. Agree with them. The recovery is a single-repository trial with no
procurement: a read-only app, their cloud account, no training on their code, pointed at the
repository they consider their worst. Ask them to name it. When they name a repository, the deal
has started.

Qualifying out. If they have fewer than 25 engineers, or development is outsourced, say so and
leave. A clean no protects the next campaign that finds them.

Handover to a human. Pricing negotiation, security review, legal, or anything a wrong answer
would cost. Escalate on the first ask, not the second.
""".strip()

KB_A_OBJECTIONS = """
Objection handling: US SaaS engineering leaders

"We tried two of these and both produced style noise." Agree, immediately and without
qualification — they are describing diff-only review accurately, and defending the category
makes us sound like the category. Then draw the distinction: style noise is what a tool produces
when it can only see the lines in the diff. Offer the narrow test: point Flightdeck at their
noisiest repository and judge it on one question, whether it catches cross-file impact. If it
does not, they have lost an hour.

"We're going to build this ourselves." Usually true and usually possible. The counter is never
difficulty. A platform team can build repository-aware review in roughly two quarters and will
then maintain it forever, against a roadmap that already has more on it than capacity. Ask what
comes off the roadmap to make room. Offer to run alongside their build if they want a baseline.

"Our code can't leave our environment." This is a qualifier, not an objection. Private VPC on
Scale, fully self-hosted inference on Enterprise, contractual no-training in the DPA. Send the
reference architecture rather than describing it.

"We already pay for a DORA tool." Do not fight it. Lead with Review, let Insights arrive as a
consolidation at their renewal date, and ask when that date is. Median customer retires 2.4
overlapping tools in the first year.

"It's not in this year's budget." Ask what would have to be true for it to be in next year's,
then ask for the measurement rather than the money: a two-week trial that produces a before and
after number costs nothing and is what the budget conversation will need anyway.

"Send me some information." Send one page, not a deck, and attach a specific next step. An
information request with no follow-up question is usually a polite exit; naming the next step
converts some of them.

"Won't this make my seniors lazier reviewers?" Honest answer: it changes what they review. The
tool takes the mechanical pass, the humans take design and intent. Teams that use it well see
review comments get shorter and more architectural.
""".strip()

KB_A_EMAIL_1 = """
Example first touch: review latency signal (US SaaS, CTO)

Subject: The 21-hour review queue post

Daniel — your post on the 21-hour review queue is the clearest write-up of that problem I have
read this year, particularly the part about senior reviewers becoming the bottleneck exactly
when you need them on architecture.

We built Flightdeck for that shape of problem. It indexes the whole repository rather than just
the diff, so it catches cross-file regressions before a human opens the PR. Hollowell Commerce,
roughly your size, went from 19 hours to 7.

Is review latency still the number one constraint now the platform team is in place, or has it
moved?

Why this works. The first sentence proves we read something specific and names the part that
matters, rather than complimenting the post generally. The second paragraph states the mechanism
("indexes the whole repository rather than just the diff") before the benefit, which is what
makes it credible to an engineer. The proof point is a named customer at comparable size with a
concrete number, not a percentage without a baseline. The close is a question about their
situation, and it contains a second piece of research — that a platform team now exists — so
even the call to action demonstrates attention.

What to vary. Swap the proof point for whichever customer is closest in size and stack. If the
prospect's public signal is a conference talk rather than a blog post, cite the specific claim
they made rather than the talk's title. Never open with the funding round.

What not to do. Do not write "I noticed you are hiring." Do not include a calendar link in a
first touch. Do not use the word "solution". Do not claim a number for their environment that
we have not measured — "went from 19 hours to 7" is a customer's result, and it must be
attributed to that customer, not implied as theirs.
""".strip()

KB_A_EMAIL_2 = """
Example first touch: change failure signal (US SaaS, CTO)

Subject: Four of five incidents inside an hour of a deploy

Marcus — your status page history for the last 60 days shows five customer-visible incidents,
and four opened within an hour of a scheduled deploy. With a PCI-regulated close product that
pattern tends to become an audit committee question before it becomes an engineering one.

Flightdeck attributes change failure rate to the individual pull request, so a post-incident
review starts from evidence instead of argument, and Guard keeps AI-written code inside the
policy you told FinTech Weekly you were drafting.

Worth twenty minutes with whoever owns the release train?

Why this works. The observation is verifiable and slightly uncomfortable, which is the tone that
gets a reply from a CTO. It connects an engineering fact to a governance consequence the
recipient already worries about, without overstating it — "tends to become" rather than "will
become". The second paragraph does two jobs: it names the mechanism, and it cites a second,
independent piece of research (the trade press quote) which proves the first was not luck.

The close asks for twenty minutes rather than a question, which is the exception to the usual
rule. It is justified here because the signal is strong enough to carry a direct ask, and
because it defers to someone else — "whoever owns the release train" — which is easier to say
yes to than a request for the CTO's own calendar.

Expected replies. The most common is a deployment and data-handling question, which is a buying
signal and should be answered with the reference architecture and the DPA rather than a call.
The second most common is silence, in which case the 72-hour follow-up should offer a number
they can measure rather than repeat the pitch.
""".strip()

KB_A_CASE_STUDY = """
Case study: Hollowell Commerce

Hollowell Commerce sells order management software to multi-channel retailers. At the time they
bought Flightdeck they were a Series C company with 240 employees and 96 engineers across seven
squads, on GitHub Enterprise with a service-oriented architecture of roughly 60 repositories.

The problem. Median time from pull request opened to first substantive review comment was 19
hours, and the distribution was worse than the median suggested: 22% of pull requests waited
more than two days. Review was concentrated in eleven senior engineers who between them were
named on 71% of all approvals. Jen Okonjo, their VP Engineering, described the effect as
"paying eleven people to be a queue". A previous diff-only review tool had been adopted and
then quietly ignored, because its comments were almost entirely stylistic.

What they did. They connected three repositories in week one, chose deliberately by picking the
two with the worst review latency and the one with the highest incident rate. Flightdeck Review
ran on every new pull request; no policy was changed and no human review was removed. After four
weeks they expanded to all 60 repositories and added Insights.

Results after one quarter. Median review latency fell from 19 hours to 7. The proportion of pull
requests waiting more than two days fell from 22% to 6%. Senior engineer participation in
routine approvals fell from 71% to 44%, with the reclaimed time visibly redistributed into
design review. Change failure rate fell from 14% to 9% over the same period, which Hollowell
attributes partly to Flightdeck and partly to a concurrent test coverage push, and they say so
in their own words rather than claiming all of it.

What they would tell a peer. Okonjo's advice to other VPs of Engineering, quoted with
permission: "Pick your worst repository, not your cleanest one. If it earns its place there it
will earn it everywhere, and if it does not you have found out in two weeks instead of two
quarters."

Hollowell now refers Flightdeck into their own network, including the introduction that opened
our Pinewell Retail Cloud opportunity.
""".strip()


KB_B_ICP = """
ICP definition: Indian BFSI technology leaders

Who qualifies. Banks, insurers, non-banking financial companies, capital markets firms and
licensed payment companies headquartered in India, with more than 1,000 total employees and,
critically, an in-house application development function of at least 120 engineers. The
headcount test is easy and the in-house test is the one that actually decides fit. India's BFSI
sector contains a large number of institutions that meet every demographic criterion and have
outsourced every line of code to a systems integrator. Those institutions have no pull requests
of their own to review and are not prospects; the integrator might be.

Who we sell to. The Chief Information Officer, Head of Information Technology, Chief Technology
Officer, or Head of Digital Technology. In most Indian financial institutions the CIO is the
economic buyer and the Head of Digital Technology or equivalent is the evaluator. Both are worth
targeting, but the evaluator replies more often and the CIO must be named in the thread.

Buying triggers. A disclosed regulatory observation on IT or change management controls, which
carries a remediation deadline and therefore a budget. A board-approved core modernisation
programme being built in house rather than bought. The opening or expansion of a captive
technology centre. A new CIO or Chief Digital Officer within two quarters. Movement into the RBI
upper layer for NBFCs, or any scope change that tightens IT governance obligations.

Regulatory context that must be understood before writing anything. RBI's IT Governance, Risk,
Controls and Assurance Practices directions; IRDAI's information and cyber security guidelines
for insurers; SEBI's Cyber Security and Cyber Resilience Framework for market participants; and
the Digital Personal Data Protection Act 2023. Getting one of these wrong in a first email ends
the conversation permanently, which is why this campaign requires two dated signals and a
confidence of 0.75 before anything sends.

Disqualifiers. Fully outsourced application development. Fewer than 1,000 employees.
Cooperative banks with no in-house technology function. Any institution currently in a public
enforcement action, which should be escalated rather than pursued.

Scoring guidance. Weight in-house engineering headcount above total headcount. Treat signal
strength conservatively: a regulatory disclosure is a strong signal, a generic careers page is
not a signal at all.
""".strip()

KB_B_PLAYBOOK = """
Sales playbook: India BFSI CIO motion

The shape of this motion. Slow, formal, evidence-led, and won by a vendor who has clearly read
the regulation before writing. Cycles run six to eighteen months, procurement is a committee,
and the first email's only job is to earn a second one. Nothing about the US motion transfers
except the discipline of citing a real signal.

Register and address. Use the honorific and surname on a first touch — "Mr Venkatesan", not
"Ramesh" — until the prospect replies using a first name. Write in complete sentences. Avoid
American idiom, contractions in the opening line, and any construction that reads as informal
familiarity. Our campaign confidence threshold is set at 0.75 specifically because tone failures
here are unrecoverable.

First touch. Email. Seventy to a hundred and twenty words. Open with the disclosed fact: an RBI
observation, an IRDAI consultation response, a board-approved programme. Connect it to an
evidence or control obligation rather than to productivity, because productivity is not what
this buyer is measured on. State the deployment posture explicitly in the first email —
in-country, in their own data centre or ap-south-1, customer-managed keys — because it is the
question that otherwise ends the conversation on reply two. Offer a document, not a meeting: a
control-mapping note or reference architecture is a far easier yes than a calendar slot.

Second touch, five days later. One follow-up only, then stop. The most effective second touch
poses a single question the prospect can put to their own team, such as whether the current
toolchain can identify, for any line in the core build, which model or engineer produced it and
which review approved it. If reconstructing that requires manual work, the gap is established
without us claiming anything.

Escalation. Every pricing question, every compliance commitment, every procurement or RFP
mention goes to a human on the first ask. No agent on this campaign may state a price, confirm
a jurisdictional guarantee, or respond to a security questionnaire.

Channels. Email and LinkedIn only. No SMS, no WhatsApp, no voice. Working hours only, in the
sending rep's India hours.
""".strip()

KB_B_OBJECTIONS = """
Objection handling: regulated Indian financial institutions

"Can you confirm in writing that no code leaves Indian jurisdiction?" This is the question that
decides the deal and it must be answered precisely, by a human, never by an agent. The truthful
answer: only the Enterprise tier with self-hosted inference can carry that commitment, because
the cloud tiers route inference to a managed model provider. In the self-hosted configuration
the index, the inference and the audit log all run on customer-operated infrastructure in India,
and we will sign to that effect. Do not soften this by implying the cloud tiers are "effectively"
in-region — two competitors have lost accounts that way and the buyer remembers.

"We already have a systems integrator who does this." Ask whether the integrator writes the code
or reviews it, and who holds the evidence when the regulator asks. Where development is genuinely
outsourced, disqualify: there is no in-house pull request to review. Where the institution builds
in house and the integrator supplements, the evidence gap is usually real and unowned.

"Our security review takes nine months." Accept it and work inside it. Send the SOC 2 Type II
report, the ISO 27001 and 27701 certificates, the penetration test summary and the DPA in the
first exchange rather than waiting to be asked. Offer a proof of concept on a non-production
repository, which in most institutions can proceed under a lighter review than a production
deployment.

"What does this cost?" Escalate. Indicative Enterprise pricing starts at ₹1.25 crore per annum
for a 300-developer estate with self-hosted inference, but no agent and no rep quotes that
without manager approval, because the residency configuration materially changes it.

"We are mid-way through a core banking programme, this is the wrong time." It is precisely the
right time and the argument is auditability, not velocity: evidence collected during the
programme is free, and evidence reconstructed afterwards is a project of its own. Frame it as
programme assurance.

"Send us your RFP response." Escalate to a human immediately. An agent must never complete a
procurement document.
""".strip()

KB_B_EMAIL_1 = """
Example first touch: regulatory remediation signal (India BFSI, CIO)

Subject: Change management remediation before March 2027

Mr Venkatesan — Anvaya's FY26 annual report commits to remediating the RBI observation on
application change management controls by March 2027, and the board has approved building the
core programme in house rather than through an integrator. Those two facts together make the
evidence trail for every code change a programme deliverable.

Corvus Flightdeck records who or what wrote each change, what reviewed it and on what basis,
entirely inside your own data centre or ap-south-1, with customer-managed keys.

If it is useful I can send the control-mapping document your audit team would need, rather than
a product overview.

Why this works. It opens with two disclosed, verifiable facts from the institution's own filings
and draws a conclusion from their combination rather than from either alone — that is the part a
CIO cannot get from a generic vendor email. It never uses the word "productivity". The
deployment posture appears in the first email, unprompted, which removes the objection that
would otherwise end the thread. The close offers a document aimed at a specific internal
audience, which is both easier to accept than a meeting and a small proof that we understand who
has to be convinced.

Register notes. Honorific and surname. No contractions in the opening sentence. No exclamation
marks, no "quick question", no calendar link. The sentence about deployment is deliberately flat
and factual: any enthusiasm in that sentence reads as overselling to this buyer.

What not to do. Do not mention a customer logo from another Indian institution without written
permission; competitive sensitivity in this sector is acute. Do not reference the regulator in a
way that implies we know more about their remediation than the public filing states.
""".strip()

KB_B_EMAIL_2 = """
Example first touch: in-house modernisation programme (India BFSI, technology head)

Subject: Evidence for software change control under the IRDAI guidelines

Deepa — Sanchay's response to the IRDAI consultation argued that the evidence burden for
software change control should be proportionate to the change, which is a more practical
position than most of the industry took.

That is the problem we work on. Flightdeck records provenance and review rationale for every
change automatically, so the evidence exists whether or not anyone remembers to produce it, and
it runs inside your ap-south-1 account with customer-managed keys.

With 40 Java engineers joining the modernisation track, is review capacity already a constraint,
or is it the evidence trail that worries you more?

Why this works. The opening cites something the recipient wrote and takes a position on it, which
is rarer and more flattering than praise. The mechanism sentence contains the phrase that matters
to a compliance-adjacent buyer — "whether or not anyone remembers to produce it" — because the
real failure mode in these institutions is not absent controls, it is controls whose evidence is
assembled retrospectively. The close offers two options and lets the prospect tell us which
problem they actually have, which is how the second email writes itself.

Register notes. First name is acceptable here because the recipient is a functional head rather
than a CXO and uses her first name publicly. The question at the end is direct but not pushy, and
it does not ask for time.

Expected replies. A pricing question, a residency question, or both — which is exactly what
happened with this prospect. Both escalate to a human on this campaign. Do not let an agent
answer either, and do not let a follow-up be sent while the escalation is open.
""".strip()

KB_B_CASE_STUDY = """
Case study: Deccan Federal Bank

Deccan Federal Bank is a private sector bank with 11,600 employees, 940 branches and an in-house
technology organisation of 430 engineers across Hyderabad and Chennai. They run Finacle as the
core with a large surrounding estate of in-house Java services, and a mainframe COBOL layer
inherited from a 1990s implementation.

The problem. A board-approved modernisation programme required 14,000 COBOL files to be migrated
to Java over three years, with an internal audit requirement that every converted file carry
evidence of review and of the basis on which it was accepted. The original plan was 40 contract
engineers for 36 months. Two independent constraints made that plan uncomfortable: the bank's
own RBI-driven change control obligations, which the contract model made harder to evidence, and
a shortage of reviewers with both COBOL and Java competence.

What they did. Deccan deployed Flightdeck Enterprise with self-hosted inference on their own
GPUs in their Hyderabad data centre. No code, prompt or metadata left the bank's perimeter at any
point, which was a precondition rather than a preference. Flightdeck Agents produced the
conversions as ordinary pull requests; Flightdeck Review checked each against the surrounding
service graph; Flightdeck Guard recorded provenance, policy results and approvals for every file.

Results. The migration backlog cleared in five months rather than 36, with two senior engineers
supervising the agent runs and a rotating panel of four reviewers handling exceptions. 14,000
files were converted; 11% were rejected by review and reworked, which the bank considers the
number that proves the process was real. The audit evidence pack was produced by export rather
than by reconstruction, and internal audit accepted it without a supplementary request — the
first time that had happened for a programme of that size.

What their CIO says. "The part that changed my mind was not the speed. It was that when internal
audit asked how a particular file had been converted and approved, we answered in about four
minutes, and the answer was the same one the system had recorded at the time."

Reference availability. Deccan will take reference calls with non-competing institutions under
NDA, arranged through our India lead. They will not be named in written material sent to another
bank without prior written approval.
""".strip()

KB_C_ICP = """
ICP definition: Seed to Series B voice AI founders

Who qualifies. Companies building voice, speech or conversational AI products — recognition,
synthesis, speech-to-speech infrastructure, voice agents, conversation analytics — at pre-seed
through Series B, with between 5 and 120 employees, headquartered in the United States, United
Kingdom, Germany, Sweden, Ireland, France or the Netherlands. Above Series B or above 120 people
they belong to the US SaaS motion and its assumptions, not this one.

Who we sell to. The founder, co-founder, CEO, CTO or Chief Scientist, and only if that person is
technical. This campaign's entire argument is mechanical and is made engineer to engineer; it
does not survive contact with a commercial buyer. A VP of Sales at a matching company is a
rejection, not a lead.

Buying triggers, in descending order. First and strongest: a founder writing publicly about
AI-assisted development, agent-authored pull requests, or review load. This is the highest
converting signal in any of our campaigns because the founder has already articulated the
problem and has nobody to delegate it to. Second: a public developer preview, API launch or
published latency or accuracy target, which means production pressure has arrived. Third: a
priced round within 120 days. Fourth: hiring a first platform or infrastructure engineer.

Disqualifiers. Post-Series B or above 120 employees. A non-technical primary contact. Agencies
and implementation partners. Companies with fewer than five engineers, where there is genuinely
not enough review volume to justify anything.

Commercial reality to hold in mind. At 8 to 25 engineers these are Team-tier deals worth $4,000
to $12,000 a year. They are worth doing anyway: this cohort adopts fast, talks publicly, and
grows into Scale within eighteen months. But a rep who discounts to win one of these has
destroyed the economics of the whole segment, so discounting is a manager decision.

Scoring guidance. Persona strength carries this campaign. A founder with a strong, recent public
post about agent code review should score into the seventies even at ten engineers, and a company
of eighty engineers with a non-technical contact should be rejected outright.
""".strip()

KB_C_PLAYBOOK = """
Sales playbook: founder motion

The shape of this motion. Fast, informal, technical, and unforgiving of anything that sounds
like marketing. Founders reply within a day or never. The campaign is configured accordingly:
follow-up gaps of 40 hours, up to four touches, and working-hours enforcement switched off
because this cohort answers email at 23:00 and does not mind.

First touch. Email, 55 to 95 words. Open with the thing they said or shipped, quoted closely
enough that they know it was read. Say what the product does mechanically in one clause. Close
with a question that is genuinely open — "is that already a constraint, or still comfortable?"
works because either answer continues the conversation, and because it gives them permission to
say no.

Tone. Write the way a senior engineer writes to another senior engineer: short sentences, no
adjectives, no "excited to", no "solution", no "leverage" as a verb. It is acceptable and often
better to concede a limitation in the first email. Founders trust a vendor who says "at eight
engineers this may not be worth it yet" far more than one who says everyone benefits.

Voice as a second channel. Voice is active on this campaign and works on founders in a way it
does not on enterprise buyers, but only after an email has landed. Never cold-call as a first
touch. If voicemail, leave nothing and send an email referencing nothing about the call. SMS is
currently paused on this campaign.

The Guard pivot. When a founder says a large share of their merged code is agent-written, stop
selling Review and sell Guard. Their problem has already moved from writing code to explaining
it, usually to a customer's security team, and the provenance record is worth more to them than
review capacity.

Pricing. Team tier, $39 per developer per month. Reps do not invent startup rates. A 12-month
seed rate with a step-up on the next round exists and is a manager decision, escalated as an
approval, never offered by an agent.

Closing. The ask is a trial, not a meeting. A founder who connects one repository has bought.
""".strip()

KB_C_OBJECTIONS = """
Objection handling: early-stage founders

"That's a lot of money for eleven people." Take it seriously, because it is. At eleven
developers Team tier is $5,148 a year, and against a seed round that is real. Do not improvise a
discount — escalate. What a rep can do immediately is reframe the comparison: the cost is
roughly one week of one engineer's fully loaded time per year, and the question is whether
review load is currently costing more than that. If they say it is not, they are probably right
and this is a conversation for after their Series A. Say so and mean it.

"We're too small for this." Sometimes true. Below five engineers, agree and leave the door open.
Between five and fifteen, the honest pitch is not capacity, it is that their two most senior
people are the review queue and that this is the specific bottleneck founders describe when they
say they have no time to build.

"We already use coding agents, we don't need more AI." Agree, and pivot. Agents write code;
Flightdeck reviews it and records where it came from. If a third of their merged pull requests
are agent-authored — which is common in this cohort — the interesting product is Guard, not
Review, because their next enterprise customer's security team will ask a question they
currently cannot answer.

"Can it run on our own infra?" Not on Team tier. Private VPC starts at Scale. Say this plainly
rather than implying otherwise; founders check.

"I'll try it when we're bigger." Counter gently: the install is cheap now and expensive later,
because the index is built against the repository's history and the review conventions are
learned from the team that exists today. There is no urgency claim to make here beyond that, so
do not manufacture one.

"Not interested / remove me." Close the thread immediately, suppress the address globally, and
send nothing further on any channel or any campaign. No confirmation email, no "sorry to hear
that". The opt-out is the end of the conversation.
""".strip()

KB_C_EMAIL_1 = """
Example first touch: agent-authored pull requests (voice AI founder)

Subject: A third of your PRs are agent-authored

Elena — your post about a third of Larkspur's merged pull requests being agent-authored, and
reviewing them being the biggest time sink, is the most honest description of that problem I
have seen from a founder.

Flightdeck reviews with a model that has indexed the whole repository, and Guard records which
model wrote what and on what basis, so the provenance exists when a hospital security team asks.
Twenty-four engineers is exactly when this is cheap to put in.

Is the review load slowing merges yet, or just annoying?

Why this works. It quotes the founder's own framing back with precision and pays a specific
compliment — "the most honest description" — rather than a generic one. The second paragraph
does two things in two clauses and then adds the detail that makes it urgent for this particular
company: they sell into hospitals, so provenance is not hypothetical. The close offers a low
and a high answer, which makes replying easy and tells us which product to lead with.

The question at the end is the most important line. "Slowing merges yet, or just annoying?"
invites the founder to downplay it, which is exactly why they answer honestly. A question that
assumes the pain is severe gets ignored.

What to vary. If the founder's public signal is a launch rather than a post about agents, lead
with the production pressure that a public API creates and hold Guard back for the reply. If
there is no public signal at all, this campaign should not be writing to them yet.

What not to do. No calendar link. No mention of the funding round unless it closed within six
months, and never as the opening line. Do not use the phrase "AI-powered".
""".strip()

KB_C_EMAIL_2 = """
Example first touch: launch and latency signal (voice AI founder)

Subject: 310ms, eleven engineers, one repo

Jonas — holding a 310ms round-trip target on a public preview with eleven engineers is the kind
of constraint where one bad merge costs a week.

Flightdeck reviews every pull request against the whole repository, so the regression that adds
40ms in a path nobody was looking at gets flagged before it lands rather than after a customer
notices. At your size it is a few minutes to install and it reviews the same day.

Is latency regression something you catch in CI already, or by feel?

Why this works. The subject line is three facts and no verb, which reads like a note from a peer
rather than an email from a vendor. The opening sentence states their constraint in their own
units — 310ms is a number they published — and draws the consequence in engineering terms. The
mechanism sentence includes a concrete, plausible failure ("adds 40ms in a path nobody was
looking at") instead of an abstraction. "A few minutes to install" is an honest and material
detail for an eleven-person team, for whom evaluation cost matters more than licence cost.

The close is the strongest part: "already, or by feel?" is a question a founder cannot answer
dishonestly without noticing, and "by feel" is the answer most of them give. This prospect
answered exactly that, then moved straight to pricing.

Expected replies. Pricing, immediately and often. At this size, escalate rather than improvise.
The second most common reply is a technical challenge about whether repository-wide review is
feasible at their latency budget, which is a good conversation and should go to a solutions
engineer rather than back to the agent.
""".strip()

KB_C_VOICE_SCRIPT = """
Voice script: founder call, first attempt (Voice AI Founders campaign)

Preconditions. Voice is a second-touch channel on this campaign and never a first touch. An
email must have been delivered at least 40 hours earlier. Call only between 09:00 and 18:00 in
the prospect's local time even though working-hours enforcement is off for email on this
campaign, because a call is not an email. Never call a number that appears on the suppression
list, and never call twice in the same week.

Opening, once they answer.

"Hi, is that Elena? — My name is Meera, I'm calling from Corvus Labs. I sent you a note on
Tuesday about agent-authored pull requests. Is now a bad time?"

Stop talking. If the answer is anything other than a clear yes, offer to call back and end the
call inside twenty seconds. A founder who is interrupted mid-thought will remember the
interruption and not the product.

If they say now is fine.

"I'll be quick. You wrote that about a third of your merged PRs are agent-authored and that
reviewing them is where your time goes. We built Flightdeck for that — it reviews with the whole
repository indexed, and it records which model wrote what, so when a customer's security team
asks you have an answer. The reason I called rather than sent another email is that I wanted to
ask one thing: is the bottleneck the reviewing, or is it explaining the output to somebody
else?"

Then listen. Whichever they say determines the product and the next step. Reviewing means
Review; explaining means Guard. Do not pitch both.

The close.

"That's useful, thank you. Can I send you a fifteen-minute setup on one repository, or would you
rather I send the provenance export format so you can look at it first?"

Voicemail. Leave nothing. Hang up and send a short email that does not mention the call.

Hard stops. Any pricing negotiation, any security commitment, any request to be removed — end
the call politely, record the outcome, and escalate. Never confirm a discount, a residency
guarantee or a roadmap date on a call.
""".strip()

KB_D_ICP = """
ICP definition: Enterprise expansion

Who qualifies. Existing Corvus Flightdeck customers on an Enterprise contract who have been
live for at least 90 days, are using Review and Insights, and have not yet purchased Guard or
Agents. This campaign never contacts a company that is not already a customer.

Who we contact. The economic buyer on the existing contract and the platform or developer
experience lead who owns day-to-day adoption. Both, in the same thread, because expansion that
is agreed with a champion and surprises the budget holder fails at renewal.

Expansion triggers, drawn from product usage rather than the open web. Review adoption above 70%
of active repositories, which indicates the deployment has settled and is trusted. Coding agent
usage detected in the customer's repositories without Guard policy coverage, which is the single
strongest expansion signal because it is a governance gap the customer has not yet noticed.
Renewal window opening within 120 days. A new regulated workload appearing in a repository that
Guard does not cover.

Disqualifiers. Accounts with an open support escalation, which should be resolved before
anything is sold. Accounts under 90 days live, where expansion pressure damages the landing.
Accounts whose champion has left without a replacement — those need a relationship rebuild, not
a campaign.

Why this campaign is still a draft. No channels are configured, which means the platform will
not let it send anything regardless of how it is otherwise set up. That is deliberate: the
product usage integration that supplies the triggers above is not yet connected, and a campaign
that cannot see adoption data would be writing generic upsell emails to customers who are paying
us. Those are the most expensive emails a company can send.

What has to be true before this goes live. The product usage connector supplying repository
adoption and agent detection, a named owner per account rather than a campaign-level sender, and
an agreed rule that any expansion conversation inside a renewal window is handed to the account
team rather than run autonomously.
""".strip()



# ---------------------------------------------------------------------------
# Helper: _fit — inline ICP fit scoring for a prospect dict, so the seed
# creates realistic ICP scores without actually calling an agent.
# ---------------------------------------------------------------------------

def _fit(score: int, rationale: str, disqualifiers: list[str] | None = None) -> dict:
    return {
        "score": score,
        "rationale": rationale,
        "disqualifiers": disqualifiers or [],
        "recommendation": "outreach" if score >= 70 else ("nurture" if score >= 40 else "disqualify"),
    }


# ---------------------------------------------------------------------------
# Seed function — idempotent, skips if data already present.
# ---------------------------------------------------------------------------

async def seed(session: AsyncSession, *, reset: bool = False) -> None:
    """Populate all tables with demo data.

    Passing ``reset=True`` wipes all mutable rows first.  Safe to call with
    the same session; caller is responsible for commit.
    """
    # ── Optional reset ──────────────────────────────────────────────────
    if reset:
        for model in [
            AuditLog, AgentRun, Message, ConversationThread, Approval,
            Signal, Prospect, Company, PromptVersion, AgentConfig,
            CampaignRep, CampaignChannel, SuppressionEntry, RateLimitCounter,
            KnowledgeChunk, KnowledgeDocument, Campaign, User, PlatformSetting,
        ]:
            await session.execute(delete(model))
        await session.flush()

    # ── Guard: already seeded? ──────────────────────────────────────────
    existing = (await session.execute(select(func.count()).select_from(User))).scalar_one()
    if existing > 0 and not reset:
        return

    # ── Platform settings ───────────────────────────────────────────────
    for key, value in [
        ("global_kill_switch", {"engaged": False, "reason": None, "engaged_by": None}),
        ("default_timezone", {"value": "Asia/Kolkata"}),
        ("working_hours_enforcement", {"enabled": True}),
        ("max_touches_per_prospect", {"value": 7}),
        ("default_reply_window_days", {"value": 3}),
        ("brand_name", {"value": "Corvus Flightdeck"}),
    ]:
        session.add(PlatformSetting(id=new_id("ps"), key=key, value=value))

    # ── Users ───────────────────────────────────────────────────────────
    for u in USERS:
        session.add(User(**{k: v for k, v in u.items()}))
    await session.flush()

    # ── Companies ───────────────────────────────────────────────────────
    companies = [
        ("co_stripe",       "Stripe",              "US",    "fintech",       5_000,  "Series I",    "https://stripe.com"),
        ("co_razorpay",     "Razorpay",            "IN",    "fintech",       3_000,  "Series F",    "https://razorpay.com"),
        ("co_notion",       "Notion",              "US",    "productivity",  1_000,  "Series C",    "https://notion.so"),
        ("co_zepto",        "Zepto",               "IN",    "e-commerce",    4_200,  "Series F",    "https://zeptonow.com"),
        ("co_plaid",        "Plaid",               "US",    "fintech",       1_200,  "Series D",    "https://plaid.com"),
        ("co_browserstack", "BrowserStack",        "IN",    "devtools",      1_800,  "PE-backed",   "https://browserstack.com"),
        ("co_groww",        "Groww",               "IN",    "wealthtech",    3_600,  "Series F",    "https://groww.in"),
        ("co_figma",        "Figma",               "US",    "design",        1_100,  "Acquired",    "https://figma.com"),
        ("co_freshworks",   "Freshworks",          "IN",    "saas",          7_000,  "Public",      "https://freshworks.com"),
        ("co_chargebee",    "Chargebee",           "IN",    "billing",         900,  "Series H",    "https://chargebee.com"),
        ("co_jupiter",      "Jupiter Money",       "IN",    "neobank",       1_000,  "Series C",    "https://jupiter.money"),
        ("co_atlassian",    "Atlassian",           "AU",    "devtools",     12_000,  "Public",      "https://atlassian.com"),
        ("co_cred",         "CRED",                "IN",    "fintech",       3_000,  "Series F",    "https://cred.club"),
        ("co_setu",         "Setu",                "IN",    "api-infra",       300,  "Acquired",    "https://setu.co"),
        ("co_postman",      "Postman",             "US",    "devtools",      1_400,  "Series D",    "https://postman.com"),
        ("co_intercom",     "Intercom",            "US",    "cx",            1_000,  "PE-backed",   "https://intercom.com"),
        ("co_vercel",       "Vercel",              "US",    "devops",          500,  "Series C",    "https://vercel.com"),
        ("co_smallcase",    "Smallcase",           "IN",    "wealthtech",      400,  "Series C",    "https://smallcase.com"),
        ("co_moengage",     "MoEngage",            "IN",    "martech",       1_200,  "Series E",    "https://moengage.com"),
        ("co_hasura",       "Hasura",              "US",    "api-infra",       350,  "Series C",    "https://hasura.io"),
    ]
    for cid, name, hq, industry, headcount, stage, website in companies:
        session.add(Company(
            id=cid, name=name, hq_country=hq, industry=industry,
            headcount=headcount, funding_stage=stage, website=website,
            linkedin_url=f"{website.rstrip('/')}/company",
        ))
    await session.flush()

    # ── Campaigns ───────────────────────────────────────────────────────
    # CMP_A — live, US SaaS CTOs
    session.add(Campaign(
        id=CMP_A,
        name="US SaaS CTO — Flightdeck Launch",
        objective="Book 8 product demos with CTOs and VPEs at 100-500 person US SaaS companies in the first 30 days of Flightdeck GA.",
        icp={
            "titles": ["CTO", "VP Engineering", "Head of Engineering", "Director of Engineering"],
            "company_size": {"min": 80, "max": 600},
            "regions": ["US"],
            "industries": ["saas", "devtools", "api-infra", "productivity"],
            "buying_signals": ["recent hiring surge", "new CTO", "Series B+", "remote-first", "github actions usage"],
        },
        product_context=PRODUCT_CONTEXT,
        status="live",
        owner_id=USR_MANAGER,
        daily_prospect_limit=20,
        max_touches_per_prospect=6,
        reply_window_days=3,
        working_hours_only=True,
        require_approval_for=["linkedin"],
        created_at=ago(days=18),
        updated_at=ago(days=1),
    ))

    # CMP_B — live, India BFSI CIOs
    session.add(Campaign(
        id=CMP_B,
        name="India BFSI CIO — Compliance Angle",
        objective="Secure 5 discovery calls with CIOs/CDOs at Indian banks, NBFCs and insurance carriers who face RBI/SEBI scrutiny on AI adoption.",
        icp={
            "titles": ["CIO", "CDO", "CTO", "Head of Technology", "Chief Digital Officer"],
            "company_size": {"min": 500, "max": 50000},
            "regions": ["IN"],
            "industries": ["fintech", "banking", "insurance", "neobank", "wealthtech"],
            "buying_signals": ["RBI circular compliance", "AI policy mandate", "digital transformation hire", "ISO 27001 renewal"],
        },
        product_context=PRODUCT_CONTEXT,
        status="live",
        owner_id=USR_MANAGER,
        daily_prospect_limit=10,
        max_touches_per_prospect=7,
        reply_window_days=5,
        working_hours_only=True,
        require_approval_for=["linkedin", "voice"],
        created_at=ago(days=12),
        updated_at=ago(hours=6),
    ))

    # CMP_C — paused, voice AI founders
    session.add(Campaign(
        id=CMP_C,
        name="Voice AI Founders — Early Adopter",
        objective="Onboard 3 voice-AI startup CTOs onto a technical design partnership — free 90-day access in exchange for co-development input.",
        icp={
            "titles": ["CTO", "Co-founder", "Founding Engineer"],
            "company_size": {"min": 5, "max": 80},
            "regions": ["US", "UK", "IN", "EU"],
            "industries": ["voice-ai", "conversational-ai", "contact-center", "speech-tech"],
            "buying_signals": ["series A", "deployed voice product", "active github", "recently hired ML engineers"],
        },
        product_context=PRODUCT_CONTEXT,
        status="paused",
        paused_at=ago(days=2),
        paused_by_id=USR_MANAGER,
        pause_reason="Founder event pipeline being reworked — resuming after YC batch announced.",
        owner_id=USR_MANAGER,
        daily_prospect_limit=8,
        max_touches_per_prospect=4,
        reply_window_days=2,
        working_hours_only=False,
        require_approval_for=[],
        created_at=ago(days=25),
        updated_at=ago(days=2),
    ))

    # CMP_D — draft, enterprise expansion
    session.add(Campaign(
        id=CMP_D,
        name="Enterprise Expansion — Guard Upsell",
        objective="Expand 15 existing Flightdeck accounts to the Guard compliance tier within the next quarter.",
        icp={
            "titles": ["CTO", "VP Engineering", "Head of Security", "CISO"],
            "company_size": {"min": 500},
            "regions": ["US", "EU"],
            "industries": ["fintech", "healthtech", "enterprise-saas"],
            "buying_signals": ["existing flightdeck customer", "renewal < 120 days", "ai coding agent detected", "regulated workload in repo"],
        },
        product_context=PRODUCT_CONTEXT,
        status="draft",
        owner_id=USR_MANAGER,
        daily_prospect_limit=5,
        max_touches_per_prospect=5,
        reply_window_days=4,
        working_hours_only=True,
        require_approval_for=["email", "linkedin", "voice"],
        created_at=ago(days=3),
        updated_at=ago(hours=1),
    ))
    await session.flush()

    # ── Campaign channels ────────────────────────────────────────────────
    for cmp_id, channels in [
        (CMP_A, [("email", 0), ("linkedin", 1)]),
        (CMP_B, [("email", 0), ("linkedin", 1), ("voice", 2)]),
        (CMP_C, [("email", 0), ("linkedin", 1)]),
        (CMP_D, [("email", 0), ("linkedin", 1), ("voice", 2)]),
    ]:
        for ch, priority in channels:
            session.add(CampaignChannel(
                id=new_id("cc"), campaign_id=cmp_id, channel=ch,
                priority=priority, paused=False,
                daily_limit=50 if ch == "email" else 15,
            ))

    # ── Campaign reps ────────────────────────────────────────────────────
    for cmp_id, rep_ids in [
        (CMP_A, [USR_RAO, USR_FERNANDES]),
        (CMP_B, [USR_IYER, USR_MANAGER]),
        (CMP_C, [USR_FERNANDES]),
        (CMP_D, [USR_RAO, USR_IYER]),
    ]:
        for rep_id in rep_ids:
            session.add(CampaignRep(id=new_id("cr"), campaign_id=cmp_id, user_id=rep_id))
    await session.flush()

    # ── Agent configs ────────────────────────────────────────────────────
    for cmp_id in [CMP_A, CMP_B, CMP_C]:
        for agent_key, model, daily_limit in [
            ("prospect_generation",    "claude-haiku-4-5",  500),
            ("research_enrichment",    "claude-haiku-4-5",  300),
            ("icp_fit",                "claude-haiku-4-5",  300),
            ("outreach_strategist",    "claude-sonnet-4-6", 100),
            ("personalisation",        "claude-sonnet-4-6", 100),
            ("reply_handler",          "claude-sonnet-4-6",  80),
        ]:
            session.add(AgentConfig(
                id=new_id("ac"), campaign_id=cmp_id, agent_key=agent_key,
                model=model, paused=False,
                daily_run_limit=daily_limit,
                temperature=0.4,
            ))
    await session.flush()

    # ── Prompt versions ──────────────────────────────────────────────────
    # Each live campaign gets all 6 active prompt versions.
    for cmp_id in [CMP_A, CMP_B]:
        for agent_key, content in DEFAULT_PROMPTS.items():
            session.add(PromptVersion(
                id=new_id("pv"), campaign_id=cmp_id, agent_key=agent_key,
                version=1, content=content, is_active=True,
                created_by=USR_MANAGER, created_at=ago(days=14),
            ))
    await session.flush()

    # ── Prospects ────────────────────────────────────────────────────────
    # 20 prospects spread across campaigns and funnel stages.
    prospects_data = [
        # (id, cmp_id, company_id, name, title, email, linkedin, stage, icp_score, identity_key)
        ("prs_001", CMP_A, "co_stripe",      "Jordan Kessler",   "VP Engineering",      "jkessler@stripe.com",      "linkedin.com/in/jkessler",    "contacted",  88, "jordan.kessler@stripe.com"),
        ("prs_002", CMP_A, "co_notion",       "Priya Haldar",     "CTO",                 "priya.h@notion.so",        "linkedin.com/in/prihahaldar", "engaged",    91, "priya.haldar@notion.so"),
        ("prs_003", CMP_A, "co_plaid",        "Ethan Moreira",    "Director Engineering","e.moreira@plaid.com",      "linkedin.com/in/ethanmoreira","researched", 76, "ethan.moreira@plaid.com"),
        ("prs_004", CMP_A, "co_postman",      "Ritika Nair",      "Head of Engineering", "ritika@postman.com",       "linkedin.com/in/ritikanair",  "qualified",  82, "ritika.nair@postman.com"),
        ("prs_005", CMP_A, "co_vercel",       "Sam Ito",          "CTO",                 "sam@vercel.com",           "linkedin.com/in/samito",      "discovered", 79, "sam.ito@vercel.com"),
        ("prs_006", CMP_A, "co_hasura",       "Tanvir Ahmed",     "VP Engineering",      "tanvir@hasura.io",         "linkedin.com/in/tanvir",      "contacted",  84, "tanvir.ahmed@hasura.io"),
        ("prs_007", CMP_A, "co_intercom",     "Claire Dubois",    "Engineering Director","claire@intercom.com",      "linkedin.com/in/clairedubois","meeting",    93, "claire.dubois@intercom.com"),
        ("prs_008", CMP_B, "co_razorpay",     "Siddharth Menon",  "CTO",                 "siddharth.m@razorpay.com", "linkedin.com/in/siddharthmenon","contacted",85, "siddharth.menon@razorpay.com"),
        ("prs_009", CMP_B, "co_groww",        "Amrita Pillai",    "CIO",                 "amrita.p@groww.in",        "linkedin.com/in/amritapillai","engaged",   90, "amrita.pillai@groww.in"),
        ("prs_010", CMP_B, "co_cred",         "Rohit Jha",        "CDO",                 "rohit.jha@cred.club",      "linkedin.com/in/rohitjha",    "researched", 72, "rohit.jha@cred.club"),
        ("prs_011", CMP_B, "co_jupiter",      "Nandita Rao",      "Head of Technology",  "nandita@jupiter.money",    "linkedin.com/in/nanditarao",  "qualified",  78, "nandita.rao@jupiter.money"),
        ("prs_012", CMP_B, "co_setu",         "Kiran Bhat",       "CTO",                 "kiran@setu.co",            "linkedin.com/in/kiranbhat",   "contacted",  81, "kiran.bhat@setu.co"),
        ("prs_013", CMP_B, "co_zepto",        "Deepak Sharma",    "VP Technology",       "deepak@zeptonow.com",      "linkedin.com/in/deepaksharma","discovered",68, "deepak.sharma@zeptonow.com"),
        ("prs_014", CMP_B, "co_smallcase",    "Ananya Krishnan",  "CTO",                 "ananya@smallcase.com",     "linkedin.com/in/ananyakr",    "meeting",    95, "ananya.krishnan@smallcase.com"),
        ("prs_015", CMP_C, "co_browserstack", "Aarav Mehta",      "Co-founder & CTO",    "aarav@browserstack.com",   "linkedin.com/in/aaravmehta",  "contacted",  88, "aarav.mehta@browserstack.com"),
        ("prs_016", CMP_C, "co_moengage",     "Sunita Pillai",    "Head of AI",          "sunita@moengage.com",      "linkedin.com/in/sunitapillai","researched", 74, "sunita.pillai@moengage.com"),
        ("prs_017", CMP_A, "co_figma",        "Lars Eriksson",    "Engineering Lead",    "lars@figma.com",           "linkedin.com/in/larseriksson","contacted",  77, "lars.eriksson@figma.com"),
        # prs_018: duplicate identity key for prs_002 — tests conflict guardrail
        ("prs_018", CMP_B, "co_notion",       "Priya Haldar",     "CTO",                 "priya.h@notion.so",        "linkedin.com/in/prihahaldar", "discovered", 65, "priya.haldar@notion.so"),
        ("prs_019", CMP_A, "co_atlassian",    "Yuki Tanaka",      "VP Engineering",      "ytanaka@atlassian.com",    "linkedin.com/in/yukitanaka",  "opportunity",97, "yuki.tanaka@atlassian.com"),
        ("prs_020", CMP_B, "co_chargebee",    "Meera Subramanian","CTO",                 "meera@chargebee.com",      "linkedin.com/in/meераsubram", "qualified",  80, "meera.subramanian@chargebee.com"),
    ]

    stage_ts = {
        "discovered":  ago(days=10),
        "researched":  ago(days=8),
        "qualified":   ago(days=6),
        "contacted":   ago(days=4),
        "engaged":     ago(days=2),
        "meeting":     ago(days=1),
        "opportunity": ago(hours=6),
        "closed":      ago(hours=1),
    }
    for pid, cmp_id, co_id, name, title, email, li, stage, score, ident_key in prospects_data:
        first, *rest = name.split()
        last = " ".join(rest)
        session.add(Prospect(
            id=pid, campaign_id=cmp_id, company_id=co_id,
            first_name=first, last_name=last,
            title=title, email=email, linkedin_url=f"https://{li}",
            stage=stage, icp_score=score,
            identity_key=ident_key,
            touches=max(0, ["discovered","researched","qualified","contacted","engaged","meeting","opportunity"].index(stage)),
            last_touched_at=stage_ts.get(stage),
            researched_at=stage_ts.get("researched") if stage not in ("discovered",) else None,
            qualified_at=stage_ts.get("qualified") if stage not in ("discovered","researched") else None,
            icp_fit=_fit(score, f"Strong match on ICP for {title} at company in target segment.", [] if score >= 75 else ["Company slightly outside target size"]),
            created_at=ago(days=12),
        ))
    await session.flush()

    # ── Signals ──────────────────────────────────────────────────────────
    signals = [
        ("sig_001", "prs_001", CMP_A, "linkedin_post",  "Jordan posted about scaling eng hiring — mentioned 40 new backend roles.", ago(days=5)),
        ("sig_002", "prs_002", CMP_A, "funding",        "Notion closed $150M Series C extension; blog post mentions AI-first engineering roadmap.", ago(days=3)),
        ("sig_003", "prs_007", CMP_A, "job_posting",    "Intercom posted 3 Senior AI Engineer roles on LinkedIn.", ago(days=2)),
        ("sig_004", "prs_009", CMP_B, "news_mention",   "Groww CIO quoted in Economic Times on RBI AI governance circular.", ago(days=1)),
        ("sig_005", "prs_014", CMP_B, "linkedin_post",  "Ananya published article: 'Why we adopted AI code review across 200 engineers'.", ago(hours=18)),
        ("sig_006", "prs_019", CMP_A, "product_usage",  "Atlassian account reached 500 weekly active users on Flightdeck trial.", ago(hours=4)),
    ]
    for sid, pid, cmp_id, stype, body, ts in signals:
        session.add(Signal(
            id=sid, prospect_id=pid, campaign_id=cmp_id,
            signal_type=stype, body=body, detected_at=ts, processed=True,
        ))
    await session.flush()

    # ── Conversation threads + messages ──────────────────────────────────
    # Thread 1: engaged prospect prs_002 (Priya Haldar, Notion)
    session.add(ConversationThread(
        id="thr_001", campaign_id=CMP_A, prospect_id="prs_002",
        channel="email", status="open", assigned_to=USR_RAO,
        last_message_at=ago(hours=3), human_takeover=False,
        created_at=ago(days=4),
    ))
    for msg_id, direction, body, ts, channel in [
        ("msg_001", "outbound",
         "Hi Priya, saw the Notion Series C news — congratulations on the AI-first engineering roadmap. "
         "We're working with a handful of SaaS CTOs to cut PR review cycle time using context-aware AI. "
         "Worth a 20-minute call this week?",
         ago(days=4), "email"),
        ("msg_002", "inbound",
         "Thanks! Yes, we're actively looking at this space. Can you send over more details on how the model handles monorepos? "
         "We have about 300 repos on GitHub.",
         ago(days=3, hours=4), "email"),
        ("msg_003", "outbound",
         "Great question — Flightdeck indexes your full org graph, not repo-by-repo, so cross-repo call chains and shared libs "
         "are in context on every review. I'll send a short technical brief. What's your engineering stack — primarily TS/Go or polyglot?",
         ago(days=3), "email"),
        ("msg_004", "inbound",
         "Mostly TypeScript and Go, with some Python for ML tooling. A brief would be great. Also — do you have any GDPR/SOC2 docs? "
         "Our legal team will ask.",
         ago(days=2, hours=6), "email"),
        ("msg_005", "outbound",
         "Attaching our SOC 2 Type II one-pager and the technical data flow diagram — both fully GDPR-mapped. "
         "I'll also include a case study from a similar-sized SaaS org (anonymised). "
         "Happy to get our Head of Security on a call with your legal team if helpful. Are you free Thursday 4–5pm IST?",
         ago(hours=3), "email"),
    ]:
        session.add(Message(
            id=msg_id, thread_id="thr_001", campaign_id=CMP_A, prospect_id="prs_002",
            direction=direction, channel=channel, body=body,
            sent_at=ts, delivered=True, opened=direction=="outbound",
            sender_id=USR_RAO if direction == "outbound" else None,
        ))

    # Thread 2: meeting booked prs_007 (Claire Dubois, Intercom)
    session.add(ConversationThread(
        id="thr_002", campaign_id=CMP_A, prospect_id="prs_007",
        channel="email", status="open", assigned_to=USR_FERNANDES,
        last_message_at=ago(hours=10), human_takeover=True,
        created_at=ago(days=6),
    ))
    for msg_id, direction, body, ts in [
        ("msg_006", "outbound",
         "Hi Claire — Intercom's 3 new AI Engineer postings caught my eye. If you're scaling AI-assisted development, "
         "we should talk: Corvus Flightdeck does repo-wide PR review that understands Intercom's architecture, not just the diff. "
         "15 minutes?",
         ago(days=6)),
        ("msg_007", "inbound",
         "Hi, yes I'm interested. We're evaluating a few tools. Can we do next Tuesday at 10am PT?",
         ago(days=5)),
        ("msg_008", "outbound",
         "Tuesday 10am PT works perfectly — I'll send a calendar invite. Looking forward to it!",
         ago(days=4, hours=22)),
    ]:
        session.add(Message(
            id=msg_id, thread_id="thr_002", campaign_id=CMP_A, prospect_id="prs_007",
            direction=direction, channel="email", body=body,
            sent_at=ts, delivered=True, opened=True,
            sender_id=USR_FERNANDES if direction == "outbound" else None,
        ))

    # Thread 3: BFSI engaged prs_009 (Amrita Pillai, Groww)
    session.add(ConversationThread(
        id="thr_003", campaign_id=CMP_B, prospect_id="prs_009",
        channel="email", status="open", assigned_to=USR_IYER,
        last_message_at=ago(hours=8), human_takeover=False,
        created_at=ago(days=5),
    ))
    for msg_id, direction, body, ts in [
        ("msg_009", "outbound",
         "Dear Amrita, the RBI's recent circular on AI governance in financial services is creating real compliance pressure "
         "for engineering teams. Corvus Flightdeck's Guard module enforces AI coding policies at the PR level — "
         "every AI-generated line is attested before it merges. Would a 20-minute conversation be valuable?",
         ago(days=5)),
        ("msg_010", "inbound",
         "This is timely. We're actually doing an internal audit this quarter. Can you share a one-pager on the Guard compliance features?",
         ago(days=4)),
        ("msg_011", "outbound",
         "Sending the Guard datasheet and a summary of how three Indian fintechs used it to satisfy their internal AI governance audits. "
         "Would you prefer a technical walkthrough with your security team or a business overview for leadership first?",
         ago(hours=8)),
    ]:
        session.add(Message(
            id=msg_id, thread_id="thr_003", campaign_id=CMP_B, prospect_id="prs_009",
            direction=direction, channel="email", body=body,
            sent_at=ts, delivered=True, opened=True,
            sender_id=USR_IYER if direction == "outbound" else None,
        ))
    await session.flush()

    # ── Approvals ────────────────────────────────────────────────────────
    approvals = [
        # (id, campaign_id, prospect_id, atype, channel, status, content, created_at, reviewed_at, reviewer_id, notes)
        ("apv_001", CMP_A, "prs_004", "outreach",  "linkedin", "pending",
         "Hi Ritika — noticed Postman's recent engineering blog on AI-assisted API design. "
         "Our platform integrates directly with your dev workflow. 15 minutes?",
         ago(hours=6), None, None, None),
        ("apv_002", CMP_B, "prs_008", "outreach",  "linkedin", "pending",
         "Namaste Siddharth — Razorpay's scale requires serious engineering governance. "
         "Flightdeck reviews every PR with full codebase context. Worth a conversation?",
         ago(hours=4), None, None, None),
        ("apv_003", CMP_B, "prs_012", "outreach",  "voice",    "pending",
         "Script: Hello, is this Kiran? This is Aditya from Corvus Labs — we help fintech CTOs enforce AI coding policies. "
         "I'm calling because Setu's API-first model would be a strong fit. Do you have 2 minutes?",
         ago(hours=2), None, None, None),
        ("apv_004", CMP_A, "prs_006", "follow_up", "email",    "pending",
         "Hi Tanvir — just bumping this up in case it got buried. "
         "Happy to share Flightdeck's Hasura-specific integration notes if that would help?",
         ago(hours=1), None, None, None),
        ("apv_005", CMP_A, "prs_001", "outreach",  "linkedin", "approved",
         "Jordan — scaling eng at Stripe is no joke. Flightdeck's cost-attribution layer "
         "gives VPEs per-PR DORA visibility. Worth 20 minutes?",
         ago(days=3), ago(days=3, hours=1), USR_MANAGER, "Good angle — approved."),
        ("apv_006", CMP_B, "prs_011", "outreach",  "linkedin", "approved",
         "Hi Nandita — Jupiter's growth story deserves an engineering platform that keeps pace. "
         "Flightdeck handles monorepo PR reviews at scale. 15 minutes?",
         ago(days=2), ago(days=2, hours=2), USR_MANAGER, None),
        ("apv_007", CMP_C, "prs_015", "outreach",  "email",    "rejected",
         "Hi Aarav — BrowserStack's infrastructure expertise makes you exactly the kind of "
         "design partner we're looking for. Interested in a co-development programme?",
         ago(days=5), ago(days=4), USR_MANAGER,
         "Too generic — needs the voice AI angle. Rewrite before sending."),
    ]
    for apv_id, cmp_id, pid, atype, channel, status, content, created, reviewed, reviewer, notes in approvals:
        session.add(Approval(
            id=apv_id, campaign_id=cmp_id, prospect_id=pid,
            approval_type=atype, channel=channel, status=status,
            content=content, created_at=created,
            reviewed_at=reviewed, reviewed_by=reviewer, review_notes=notes,
        ))
    await session.flush()

    # ── Suppression list ─────────────────────────────────────────────────
    for sid, email, reason, scope in [
        ("sup_001", "karan.sharma@corvuslabs.ai", "internal team member", "global"),
        ("sup_002", "legal@atlassian.com",        "legal team — do not contact", "global"),
        ("sup_003", "noreply@notion.so",           "automated address", "global"),
    ]:
        session.add(SuppressionEntry(
            id=sid, email=email, reason=reason, scope=scope,
            added_by=USR_MANAGER, added_at=ago(days=20),
        ))
    await session.flush()

    # ── Agent run history (representative sample) ─────────────────────────
    run_pairs = [
        ("run_001", CMP_A, "prs_001", "research_enrichment", "succeeded", ago(days=9)),
        ("run_002", CMP_A, "prs_001", "icp_fit",             "succeeded", ago(days=9)),
        ("run_003", CMP_A, "prs_001", "outreach_strategist", "succeeded", ago(days=8)),
        ("run_004", CMP_A, "prs_001", "personalisation",     "succeeded", ago(days=8)),
        ("run_005", CMP_A, "prs_002", "research_enrichment", "succeeded", ago(days=8)),
        ("run_006", CMP_A, "prs_002", "icp_fit",             "succeeded", ago(days=8)),
        ("run_007", CMP_A, "prs_007", "personalisation",     "succeeded", ago(days=5)),
        ("run_008", CMP_B, "prs_009", "research_enrichment", "succeeded", ago(days=4)),
        ("run_009", CMP_B, "prs_009", "icp_fit",             "succeeded", ago(days=4)),
        ("run_010", CMP_A, "prs_003", "research_enrichment", "failed",    ago(days=7)),
    ]
    for run_id, cmp_id, pid, agent_key, status, ts in run_pairs:
        session.add(AgentRun(
            id=run_id, campaign_id=cmp_id, prospect_id=pid,
            agent_key=agent_key,
            idempotency_key=f"{cmp_id}:{pid}:{agent_key}:v1",
            status=status,
            model="claude-haiku-4-5" if agent_key in ("research_enrichment","icp_fit") else "claude-sonnet-4-6",
            executor="simulator",
            started_at=ts,
            finished_at=ts + dt.timedelta(seconds=2),
            latency_ms=1800 if status == "succeeded" else 800,
            cost_usd=0.0008 if status == "succeeded" else 0.0,
            output={"status": "ok"} if status == "succeeded" else None,
            error="Transport timeout" if status == "failed" else None,
        ))
    await session.flush()

    # ── Audit log (key system events) ────────────────────────────────────
    audit_events = [
        ("Campaign CMP_A created",          "campaign", CMP_A, "campaign_created",    "info",  USR_MANAGER, ago(days=18)),
        ("Campaign CMP_B created",          "campaign", CMP_B, "campaign_created",    "info",  USR_MANAGER, ago(days=12)),
        ("Campaign CMP_C paused",           "campaign", CMP_C, "campaign_paused",     "warn",  USR_MANAGER, ago(days=2)),
        ("Prospect prs_007 reached meeting","prospect",  "prs_007", "stage_advanced", "info",  "system",    ago(days=1)),
        ("Prospect prs_019 is opportunity", "prospect",  "prs_019", "stage_advanced", "info",  "system",    ago(hours=6)),
        ("Approval apv_007 rejected",       "approval",  "apv_007", "approval_rejected","warn", USR_MANAGER, ago(days=4)),
        ("Inbound reply from prs_002",      "message",   "msg_002", "inbound_received","info",  "system",    ago(days=3, hours=4)),
        ("Agent run_010 failed",            "agent_run", "run_010", "agent_failed",    "error", "system",    ago(days=7)),
    ]
    for message, entity_type, entity_id, event_type, severity, actor, ts in audit_events:
        session.add(AuditLog(
            id=new_id("al"), entity_type=entity_type, entity_id=entity_id,
            event_type=event_type, severity=severity, message=message,
            actor=actor, occurred_at=ts,
        ))
    await session.flush()

    # ── Knowledge base — ingest via normal RAG pipeline ──────────────────
    # Pull the big doc strings already defined above in this module.
    import sys
    this = sys.modules[__name__]
    doc_map = {
        "product_overview":   ("PRODUCT_OVERVIEW_DOC",   "product_overview", "Corvus Flightdeck — Product Overview"),
        "icp_us_saas":        ("ICP_US_SAAS_DOC",        "icp",              "ICP: US SaaS CTO (Campaign A)"),
        "icp_india_bfsi":     ("ICP_INDIA_BFSI_DOC",     "icp",              "ICP: India BFSI CIO (Campaign B)"),
        "icp_voice_founders": ("ICP_VOICE_DOC",          "icp",              "ICP: Voice AI Founders (Campaign C)"),
        "playbook_us_saas":   ("PLAYBOOK_US_SAAS_DOC",   "playbook",         "Outreach Playbook: US SaaS CTO"),
        "playbook_india_bfsi":("PLAYBOOK_INDIA_BFSI_DOC","playbook",         "Outreach Playbook: India BFSI CIO"),
        "objection_handling": ("OBJECTION_DOC",          "playbook",         "Objection Handling Guide"),
        "case_studies":       ("CASE_STUDIES_DOC",       "case_study",       "Customer Case Studies"),
        "expansion_icp":      ("EXPANSION_ICP_DOC",      "icp",              "ICP: Enterprise Expansion (Campaign D)"),
    }
    # Assign docs to campaigns (some are platform-wide, some campaign-specific)
    doc_campaign_map = {
        "product_overview":   [CMP_A, CMP_B, CMP_C, CMP_D],
        "icp_us_saas":        [CMP_A],
        "icp_india_bfsi":     [CMP_B],
        "icp_voice_founders": [CMP_C],
        "playbook_us_saas":   [CMP_A],
        "playbook_india_bfsi":[CMP_B],
        "objection_handling": [CMP_A, CMP_B, CMP_C, CMP_D],
        "case_studies":       [CMP_A, CMP_B, CMP_C, CMP_D],
        "expansion_icp":      [CMP_D],
    }
    for doc_key, (attr_name, doc_type, title) in doc_map.items():
        content = getattr(this, attr_name, None)
        if not content:
            continue
        for cmp_id in doc_campaign_map.get(doc_key, []):
            await ingest_document(
                session,
                campaign_id=cmp_id,
                doc_type=doc_type,
                title=title,
                content=content,
                source=f"seed:{doc_key}",
                author=USR_MANAGER,
            )

    await session.flush()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

async def main(reset: bool = False) -> None:
    await init_models()
    async with session_scope() as session:
        await seed(session, reset=reset)
        await session.commit()
    print("✓ Seed complete.")


if __name__ == "__main__":
    import sys
    reset = "--reset" in sys.argv
    asyncio.run(main(reset=reset))

