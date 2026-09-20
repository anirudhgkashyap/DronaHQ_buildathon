"""
seed_minimal.py — fast, correct seed for hackathon demo.
Run: python -m app.seed_minimal

All enum-like string fields below are set to the values the ORM models and
API routes actually validate against (see app/models.py comments):
  ConversationThread.status : active | awaiting_reply | escalated | closed | meeting_booked
  Prospect.stage            : discovered | researched | qualified | contacted | engaged | meeting | opportunity
  Prospect.status           : pending | researching | awaiting_approval | in_outreach | replied |
                               meeting_booked | rejected | closed | escalated
  AgentRun.status            : pending | running | succeeded | failed | skipped
  Agent keys                 : prospect_generation | research_enrichment | icp_fit |
                                outreach_strategy | personalisation | conversation
"""
from __future__ import annotations

import asyncio
import datetime as dt

from .agents.schemas import CAMPAIGN_SYSTEM_PROMPT, default_prompt
from .db import init_models, new_id, session_scope
from .models import (
    AGENT_KEYS,
    AgentConfig,
    AgentRun,
    Approval,
    Campaign,
    CampaignChannel,
    Company,
    ConversationThread,
    KnowledgeDocument,
    Message,
    PlatformSetting,
    Prospect,
    PromptVersion,
    User,
)

CAMPAIGN_PROMPT_KEY = "__campaign__"

# ── Fixed IDs so URLs are stable across reseeds ──────────────────────────────
USR_ADMIN   = "usr_admin001"
USR_MGR     = "usr_mgr0001"
CMP_A       = "cmp_usaas01"
CMP_B       = "cmp_india01"
CO_STRIPE   = "co_stripe01"
CO_NOTION   = "co_notion01"
CO_RAZORPAY = "co_razrpy01"
CO_LINEAR   = "co_linear01"


async def seed(reset: bool = False) -> None:
    await init_models()

    async with session_scope() as s:
        if reset:
            # clear leaf tables first to respect FK order
            import sqlalchemy
            for model in [Message, Approval, AgentRun, ConversationThread,
                          AgentConfig, PromptVersion, Prospect, Company,
                          CampaignChannel, Campaign, KnowledgeDocument,
                          PlatformSetting, User]:
                await s.execute(sqlalchemy.delete(model))

        # ── Platform settings ────────────────────────────────────────────────
        for key, value in [
            ("kill_switch",              {"engaged": False}),
            ("global_daily_send_cap",    {"value": 500}),
            ("dry_run",                  {"value": False}),
            ("working_hours",            {"start": "09:00", "end": "18:00"}),
            ("working_days",             {"days": ["mon", "tue", "wed", "thu", "fri"]}),
            ("company_name",             {"value": "Atlas SDR"}),
            ("timezone",                 {"value": "Asia/Kolkata"}),
            ("channels",                 {
                "email": {"from_email": "sdr@atlas.example", "from_name": "Atlas SDR", "reply_to": "hello@atlas.example"},
                "sms": {"number": ""},
                "linkedin": {"daily_limit": 20},
            }),
        ]:
            s.add(PlatformSetting(key=key, value=value))

        # ── Users ────────────────────────────────────────────────────────────
        s.add(User(
            id=USR_ADMIN, name="Daksh Agrawal", initials="DA",
            email="dakshagrawal1324@gmail.com", role="admin", active=True,
            daily_activity_limit=200,
            working_hours={"start": "09:00", "end": "18:00"},
            channels_available=["email", "linkedin"],
        ))
        s.add(User(
            id=USR_MGR, name="S. Menon", initials="SM",
            email="s.menon@acme.com", role="manager", active=True,
            daily_activity_limit=150,
            working_hours={"start": "09:00", "end": "18:00"},
            channels_available=["email", "sms", "linkedin"],
        ))
        await s.flush()

        # ── Campaigns ────────────────────────────────────────────────────────
        s.add(Campaign(
            id=CMP_A, name="US SaaS · CTO Outreach", label="US SaaS",
            description="Book demos with CTOs at 100-500 person US SaaS companies.",
            icp_summary="SaaS, Series B–D, CTO / VP Eng, 50–500 FTE",
            objective="8 demos in 30 days",
            status="live", owner_id=USR_ADMIN,
            icp={
                "titles": ["CTO", "VP Engineering", "Head of Engineering"],
                "company_size": {"min": 80, "max": 600},
                "regions": ["US"],
                "industries": ["saas", "devtools", "productivity"],
            },
            product_context={"summary": "Corvus Flightdeck — AI-native release pipeline."},
            policies={"max_touches": 6, "reply_window_days": 3, "daily_limit": 20, "approval_threshold": 0.8, "dry_run": True},
        ))
        s.add(Campaign(
            id=CMP_B, name="India BFSI · CIO Outreach", label="India BFSI",
            description="Reach CIOs at Indian BFSI companies for enterprise deals.",
            icp_summary="BFSI, CIO / Head of IT, 500+ FTE",
            objective="5 qualified meetings",
            status="paused", owner_id=USR_MGR,
            icp={
                "titles": ["CIO", "CTO", "VP Technology"],
                "company_size": {"min": 500, "max": 10000},
                "regions": ["IN"],
                "industries": ["fintech", "banking", "insurance"],
            },
            product_context={"summary": "Corvus Flightdeck for regulated industries."},
            policies={"max_touches": 5, "reply_window_days": 5, "daily_limit": 15, "approval_threshold": 0.8, "dry_run": True},
        ))
        await s.flush()

        # ── Channels ─────────────────────────────────────────────────────────
        for cmp_id in [CMP_A, CMP_B]:
            for ch_type in ["email", "linkedin"]:
                s.add(CampaignChannel(
                    id=new_id("cc"), campaign_id=cmp_id, type=ch_type,
                    state="active", daily_limit=20 if ch_type == "email" else 10,
                    config={},
                ))
        await s.flush()

        # ── Companies ────────────────────────────────────────────────────────
        for co_id, name, domain, industry, headcount, stage, hq in [
            (CO_STRIPE,   "Stripe",   "stripe.com",   "fintech",      5000, "Series I", "San Francisco, US"),
            (CO_NOTION,   "Notion",   "notion.so",    "productivity", 1000, "Series C", "San Francisco, US"),
            (CO_LINEAR,   "Linear",   "linear.app",   "devtools",      200, "Series B", "San Francisco, US"),
            (CO_RAZORPAY, "Razorpay", "razorpay.com", "fintech",      3000, "Series F", "Bengaluru, IN"),
        ]:
            # Companies require campaign_id — put US companies in CMP_A, IN in CMP_B
            cmp = CMP_B if hq.endswith(", IN") else CMP_A
            s.add(Company(
                id=co_id, campaign_id=cmp, name=name, domain=domain,
                industry=industry, headcount=headcount, funding_stage=stage,
                hq_location=hq, enrichment={},
            ))
        await s.flush()

        # ── Prospects ────────────────────────────────────────────────────────
        # stage  : discovered | researched | qualified | contacted | engaged | meeting | opportunity
        # status : pending | researching | awaiting_approval | in_outreach | replied |
        #          meeting_booked | rejected | closed | escalated
        P1 = new_id("pr"); P2 = new_id("pr"); P3 = new_id("pr"); P4 = new_id("pr")
        prospects = [
            # id, campaign, company, name,           title,                  email,                 score, stage,       status
            (P1, CMP_A, CO_STRIPE,   "Alice Chen",   "CTO",                  "alice@stripe.com",   92, "engaged",    "replied"),
            (P2, CMP_A, CO_NOTION,   "Carol Kim",    "CTO",                  "carol@notion.so",    95, "engaged",    "replied"),
            (P3, CMP_A, CO_LINEAR,   "David Singh",  "Head of Engineering",  "david@linear.app",   65, "contacted",  "awaiting_approval"),
            (P4, CMP_B, CO_RAZORPAY, "Priya Sharma", "VP Technology",        "priya@razorpay.com", 81, "contacted",  "in_outreach"),
        ]
        for pid, cid, coid, name, title, email, score, stage, status in prospects:
            s.add(Prospect(
                id=pid, campaign_id=cid, company_id=coid,
                full_name=name, designation=title, email=email,
                seniority="director" if "VP" in title else "c_suite",
                stage=stage, status=status,
                fit_score=score, fit_verdict="qualified" if score >= 80 else "borderline",
                fit_rationale=f"Strong ICP match — {title} at relevant company.",
                fit_criteria={}, research={}, channels_tried=["email"],
            ))
        await s.flush()

        # ── Conversations & messages ─────────────────────────────────────────
        # ConversationThread.status: active | awaiting_reply | escalated | closed | meeting_booked
        now = dt.datetime.now(dt.timezone.utc)
        THREAD_1 = new_id("th"); THREAD_2 = new_id("th")

        s.add(ConversationThread(
            id=THREAD_1, campaign_id=CMP_A, prospect_id=P1,
            channel="email", status="awaiting_reply", sentiment="positive",
            last_activity_at=now - dt.timedelta(minutes=5),
        ))
        s.add(ConversationThread(
            id=THREAD_2, campaign_id=CMP_A, prospect_id=P2,
            channel="email", status="meeting_booked", sentiment="positive",
            last_activity_at=now - dt.timedelta(hours=2),
        ))
        await s.flush()

        # Messages for thread 1 (Alice — replied, awaiting our response)
        s.add(Message(
            id=new_id("msg"), thread_id=THREAD_1, campaign_id=CMP_A, prospect_id=P1,
            channel="email", direction="outbound", message_type="initial_outreach",
            subject="Quick question for Alice",
            body="Hi Alice,\n\nI noticed Stripe is scaling its engineering org rapidly. We help CTOs at Series B–D SaaS companies cut their release cycle by 40%.\n\nWould a 20-minute call next week make sense?\n\nBest,\nDaksh",
            status="delivered", idempotency_key=new_id("ik"),
            sent_at=now - dt.timedelta(days=3),
        ))
        s.add(Message(
            id=new_id("msg"), thread_id=THREAD_1, campaign_id=CMP_A, prospect_id=P1,
            channel="email", direction="inbound", message_type="reply",
            subject="Re: Quick question for Alice",
            body="Thanks for reaching out! I'd love to connect — what does your platform actually do? Can you send more detail?",
            status="delivered", idempotency_key=new_id("ik"),
            sent_at=now - dt.timedelta(minutes=5),
        ))

        # Messages for thread 2 (Carol — replied, meeting booked)
        s.add(Message(
            id=new_id("msg"), thread_id=THREAD_2, campaign_id=CMP_A, prospect_id=P2,
            channel="email", direction="outbound", message_type="initial_outreach",
            subject="For Carol @ Notion",
            body="Hi Carol,\n\nNotion's engineering team has grown fast this year. We help CTOs at similar-stage SaaS companies ship releases 40% faster.\n\nOpen to a quick call?\n\nBest,\nDaksh",
            status="delivered", idempotency_key=new_id("ik"),
            sent_at=now - dt.timedelta(days=5),
        ))
        s.add(Message(
            id=new_id("msg"), thread_id=THREAD_2, campaign_id=CMP_A, prospect_id=P2,
            channel="email", direction="inbound", message_type="reply",
            subject="Re: For Carol @ Notion",
            body="Sounds interesting — let's talk. Meeting booked for Tuesday at 10am PT.",
            status="delivered", idempotency_key=new_id("ik"),
            sent_at=now - dt.timedelta(hours=2),
        ))

        # ── Approvals ────────────────────────────────────────────────────────
        APR1 = new_id("apr")
        s.add(Approval(
            id=APR1, campaign_id=CMP_A, prospect_id=P3,
            title="Follow-up email — David Singh",
            reason="Low confidence",
            detail="Outreach Strategy Agent wants to send a follow-up but confidence is 0.72 (threshold 0.80). Review before proceeding.",
            status="pending",
        ))
        s.add(Approval(
            id=new_id("apr"), campaign_id=CMP_A, prospect_id=P2,
            title="LinkedIn connection — Carol Kim",
            reason="Needs human reply",
            detail="Personalisation Agent proposes switching to LinkedIn after 2 email touches with no reply.",
            status="pending",
        ))
        await s.flush()

        # ── Agent configs + campaign system prompt (makes campaigns "runnable") ─
        for cmp_id in [CMP_A, CMP_B]:
            s.add(PromptVersion(
                id=new_id("pv"), campaign_id=cmp_id, agent_key=CAMPAIGN_PROMPT_KEY,
                version=1, is_active=True, created_by_id=USR_ADMIN,
                content=CAMPAIGN_SYSTEM_PROMPT,
                notes="Seeded with the platform default campaign system prompt.",
                activated_at=now,
            ))
            for agent_key in AGENT_KEYS:
                s.add(AgentConfig(
                    id=new_id("acfg"), campaign_id=cmp_id, agent_key=agent_key,
                    enabled=True, state="active",
                ))
        await s.flush()

        # ── Agent runs (status: pending | running | succeeded | failed | skipped) ─
        for agent_key, status_, cost in [
            ("prospect_generation", "succeeded", 0.0018),
            ("research_enrichment", "succeeded", 0.0042),
            ("icp_fit",             "succeeded", 0.0009),
            ("outreach_strategy",   "succeeded", 0.0031),
            ("personalisation",     "succeeded", 0.0055),
            ("conversation",        "succeeded", 0.0014),
        ]:
            s.add(AgentRun(
                id=new_id("ar"), campaign_id=CMP_A, prospect_id=P1,
                agent_key=agent_key, status=status_,
                idempotency_key=new_id("ik"),
                input={"prospect_id": P1},
                output={"ok": True},
                tokens_in=800, tokens_out=200, cost_usd=cost, latency_ms=1200,
                started_at=now - dt.timedelta(hours=1),
                finished_at=now - dt.timedelta(hours=1) + dt.timedelta(seconds=2),
            ))

        # ── Prompt versions (agent-level, per campaign) ─────────────────────
        s.add(PromptVersion(
            id=new_id("pv"), campaign_id=CMP_A, agent_key="personalisation",
            version=6, is_active=True, created_by_id=USR_ADMIN,
            content="You are a world-class B2B SDR. Write a hyper-personalised outreach email for {{first_name}} at {{company}}. Lead with a relevant signal. Keep it under 100 words. No fluff.",
            notes="Tighter word count, signal-first",
            activated_at=now - dt.timedelta(minutes=200),
        ))
        s.add(PromptVersion(
            id=new_id("pv"), campaign_id=CMP_A, agent_key="personalisation",
            version=5, is_active=False, created_by_id=USR_ADMIN,
            content="You are a B2B SDR. Your goal is to book a meeting with {{first_name}} at {{company}}.",
            notes="Earlier version, superseded by v6",
        ))
        s.add(PromptVersion(
            id=new_id("pv"), campaign_id=CMP_A, agent_key="conversation",
            version=3, is_active=True, created_by_id=USR_ADMIN,
            content="Classify this inbound reply as one of: interested, objection, unsubscribe, out_of_office, other. Reply with JSON: {\"intent\": \"...\", \"confidence\": 0.0-1.0, \"suggested_action\": \"...\"}",
            notes="Added confidence score",
            activated_at=now - dt.timedelta(minutes=480),
        ))

        await s.flush()
        print("✅ Seed complete!")
        print("   Campaigns: 2  |  Prospects: 4  |  Threads: 2  |  Approvals: 2  |  Agent runs: 6")


async def main(reset: bool = True) -> None:
    await seed(reset=reset)


if __name__ == "__main__":
    import sys
    reset = "--reset" in sys.argv or True
    asyncio.run(main(reset=reset))
