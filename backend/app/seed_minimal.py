"""
seed_minimal.py — fast, correct seed for hackathon demo.
Run: python -m app.seed_minimal
"""
from __future__ import annotations

import asyncio
import datetime as dt

from .db import init_models, new_id, session_scope
from .models import (
    User, Campaign, CampaignChannel, Company, Prospect,
    ConversationThread, Message, Approval, AgentRun,
    PromptVersion, PlatformSetting, KnowledgeDocument,
)

# Fixed IDs so URLs are stable across reseeds
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
            for model in [Message, Approval, AgentRun, ConversationThread,
                          Prospect, Company, CampaignChannel, Campaign,
                          PromptVersion, KnowledgeDocument, PlatformSetting, User]:
                await s.execute(__import__("sqlalchemy").delete(model))

        for key, value in [
            ("kill_switch",           {"engaged": False}),
            ("global_daily_send_cap", {"value": 500}),
            ("dry_run",               {"value": False}),
            ("working_hours",         {"start": "09:00", "end": "18:00"}),
            ("working_days",          {"days": ["mon","tue","wed","thu","fri"]}),
        ]:
            s.add(PlatformSetting(key=key, value=value))

        s.add(User(id=USR_ADMIN, name="Daksh Agrawal", initials="DA",
                   email="dakshagrawal1324@gmail.com", role="admin", active=True,
                   daily_activity_limit=200,
                   working_hours={"start":"09:00","end":"18:00"},
                   channels_available=["email","linkedin"]))
        s.add(User(id=USR_MGR, name="S. Menon", initials="SM",
                   email="s.menon@acme.com", role="manager", active=True,
                   daily_activity_limit=150,
                   working_hours={"start":"09:00","end":"18:00"},
                   channels_available=["email","sms","linkedin"]))
        await s.flush()

        s.add(Campaign(id=CMP_A, name="US SaaS · CTO Outreach", label="US SaaS",
                       description="Book demos with CTOs at 100-500 person US SaaS companies.",
                       objective="8 demos in 30 days", status="live", owner_id=USR_ADMIN,
                       icp={"titles":["CTO","VP Engineering"],"company_size":{"min":80,"max":600},
                            "regions":["US"],"industries":["saas","devtools"]},
                       product_context="Corvus Flightdeck — AI-native release pipeline.",
                       policies={"max_touches":6,"reply_window_days":3,"daily_limit":20}))
        s.add(Campaign(id=CMP_B, name="India BFSI · CIO Outreach", label="India BFSI",
                       description="Reach CIOs at Indian BFSI companies.",
                       objective="5 qualified meetings", status="paused", owner_id=USR_MGR,
                       icp={"titles":["CIO","CTO","VP Technology"],
                            "company_size":{"min":500,"max":10000},
                            "regions":["IN"],"industries":["fintech","banking"]},
                       product_context="Corvus Flightdeck for regulated industries.",
                       policies={"max_touches":5,"reply_window_days":5,"daily_limit":15}))
        await s.flush()

        for cmp_id in [CMP_A, CMP_B]:
            for ch_type in ["email","linkedin"]:
                s.add(CampaignChannel(id=new_id("cc"), campaign_id=cmp_id, type=ch_type,
                                      state="active",
                                      daily_limit=20 if ch_type=="email" else 10, config={}))
        await s.flush()

        for co_id, name, domain, industry, headcount, stage, hq in [
            (CO_STRIPE,   "Stripe",   "stripe.com",   "fintech",      5000,"Series I","San Francisco, US"),
            (CO_NOTION,   "Notion",   "notion.so",    "productivity", 1000,"Series C","San Francisco, US"),
            (CO_LINEAR,   "Linear",   "linear.app",   "devtools",      200,"Series B","San Francisco, US"),
            (CO_RAZORPAY, "Razorpay", "razorpay.com", "fintech",      3000,"Series F","Bengaluru, IN"),
        ]:
            cmp = CMP_B if hq.endswith(", IN") else CMP_A
            s.add(Company(id=co_id, campaign_id=cmp, name=name, domain=domain,
                          industry=industry, headcount=headcount, funding_stage=stage,
                          hq_location=hq, enrichment={}))
        await s.flush()

        P1=new_id("pr"); P2=new_id("pr"); P3=new_id("pr"); P4=new_id("pr")
        for pid, cid, coid, name, title, email, score, stage in [
            (P1,CMP_A,CO_STRIPE,  "Alice Chen","CTO","alice@stripe.com",92,"replied"),
            (P2,CMP_A,CO_NOTION,  "Carol Kim","CTO","carol@notion.so",95,"replied"),
            (P3,CMP_A,CO_LINEAR,  "David Singh","Head of Engineering","david@linear.app",65,"contacted"),
            (P4,CMP_B,CO_RAZORPAY,"Priya Sharma","VP Technology","priya@razorpay.com",81,"contacted"),
        ]:
            s.add(Prospect(id=pid, campaign_id=cid, company_id=coid, full_name=name,
                           designation=title, email=email,
                           seniority="director" if "VP" in title else "c_suite",
                           stage=stage, status="active", fit_score=score,
                           fit_verdict="strong" if score>=80 else "moderate",
                           fit_rationale=f"Strong ICP match — {title}.",
                           fit_criteria={}, research={}, channels_tried=["email"]))
        await s.flush()

        now = dt.datetime.now(dt.timezone.utc)
        T1=new_id("th"); T2=new_id("th")
        s.add(ConversationThread(id=T1, campaign_id=CMP_A, prospect_id=P1,
                                 channel="email", status="replied", sentiment="positive",
                                 last_activity_at=now-dt.timedelta(minutes=5)))
        s.add(ConversationThread(id=T2, campaign_id=CMP_A, prospect_id=P2,
                                 channel="email", status="replied", sentiment="neutral",
                                 last_activity_at=now-dt.timedelta(hours=2)))
        await s.flush()

        s.add(Message(id=new_id("msg"), thread_id=T1, campaign_id=CMP_A, prospect_id=P1,
                      channel="email", direction="outbound", message_type="initial",
                      subject="Quick question for Alice",
                      body="Hi Alice,\n\nI noticed Stripe is scaling rapidly. We help CTOs cut release cycles by 40%.\n\nWould a 20-minute call make sense?\n\nBest, Daksh",
                      status="delivered", idempotency_key=new_id("ik"),
                      sent_at=now-dt.timedelta(days=3)))
        s.add(Message(id=new_id("msg"), thread_id=T1, campaign_id=CMP_A, prospect_id=P1,
                      channel="email", direction="inbound", message_type="reply",
                      subject="Re: Quick question for Alice",
                      body="Thanks for reaching out! I'd love to connect — what does your platform actually do?",
                      status="received", idempotency_key=new_id("ik"),
                      sent_at=now-dt.timedelta(minutes=5)))

        s.add(Approval(id=new_id("apr"), campaign_id=CMP_A, prospect_id=P3,
                       title="Follow-up email — David Singh",
                       reason="confidence_below_threshold",
                       detail="Outreach Strategist confidence 0.72 < threshold 0.80. Review before proceeding.",
                       status="pending"))
        s.add(Approval(id=new_id("apr"), campaign_id=CMP_A, prospect_id=P2,
                       title="LinkedIn connection — Carol Kim",
                       reason="new_channel",
                       detail="Personalisation Agent proposes LinkedIn after 2 email touches.",
                       status="pending"))

        for agent_key, cost in [
            ("prospect_generation",0.0018),("research_enrichment",0.0042),
            ("icp_fit",0.0009),("outreach_strategist",0.0031),
            ("personalisation",0.0055),("reply_handler",0.0014),
        ]:
            s.add(AgentRun(id=new_id("ar"), campaign_id=CMP_A, prospect_id=P1,
                           agent_key=agent_key, status="completed",
                           idempotency_key=new_id("ik"), input={"prospect_id":P1},
                           output={"ok":True}, tokens_in=800, tokens_out=200,
                           cost_usd=cost, latency_ms=1200,
                           started_at=now-dt.timedelta(hours=1),
                           finished_at=now-dt.timedelta(hours=1)+dt.timedelta(seconds=2)))

        s.add(PromptVersion(id=new_id("pv"), campaign_id=CMP_A, agent_key="personalisation",
                            version=6, is_active=True, created_by_id=USR_ADMIN,
                            content="You are a world-class B2B SDR. Write a hyper-personalised outreach email for {{first_name}} at {{company}}. Lead with a signal. Under 100 words.",
                            notes="Tighter word count, signal-first"))
        s.add(PromptVersion(id=new_id("pv"), campaign_id=CMP_A, agent_key="reply_handler",
                            version=3, is_active=True, created_by_id=USR_ADMIN,
                            content="Classify this reply: interested | objection | unsubscribe | out_of_office | other. Reply JSON: {\"intent\":\"...\",\"confidence\":0.0}",
                            notes="Added confidence"))
        await s.flush()
        print("✅ Seed complete!")
        print("   Campaigns: 2  |  Prospects: 4  |  Threads: 2  |  Approvals: 2  |  Agent runs: 6")


async def main(reset: bool = True) -> None:
    await seed(reset=reset)


if __name__ == "__main__":
    import sys
    asyncio.run(main(reset=True))
