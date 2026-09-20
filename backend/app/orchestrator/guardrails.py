"""The guardrail layer: the single chokepoint every autonomous external
action passes through.

This is deliberately NOT an LLM agent. Whether a message may leave the system
is a policy question with a correct answer, and a policy question should be
decided by code that is auditable and cannot be talked out of its decision by
a cleverly worded prompt.

Four independent levels of operational control are enforced here, in
increasing scope:

    channel pause   -> stops one channel on one campaign
    agent pause     -> stops one agent on one campaign
    campaign pause  -> stops one campaign entirely
    global kill     -> stops every autonomous external action, platform-wide

Order matters. The cheapest and broadest checks run first so that a kill
switch short-circuits before anything touches a model or a provider.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Optional, Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import new_id
from ..models import (
    AgentConfig,
    Campaign,
    CampaignChannel,
    Message,
    Prospect,
    RateLimitCounter,
    SuppressionEntry,
    utcnow,
)

KILL_SWITCH_KEY = "kill_switch"


@dataclass
class Decision:
    """The verdict on one proposed action.

    ``code`` is stable and machine-readable so the UI can group blocks by
    cause; ``reason`` is written for a sales manager to read in a toast.
    """

    allowed: bool
    code: str = "ok"
    reason: str = ""
    retry_after: Optional[dt.datetime] = None
    context: dict = field(default_factory=dict)

    def __bool__(self) -> bool:  # lets callers write `if decision:`
        return self.allowed

    @classmethod
    def ok(cls, **context) -> "Decision":
        return cls(allowed=True, context=context)

    @classmethod
    def block(cls, code: str, reason: str, **context) -> "Decision":
        return cls(allowed=False, code=code, reason=reason, context=context)


# ---------------------------------------------------------------------------
# Platform state
# ---------------------------------------------------------------------------
async def get_kill_switch(session: AsyncSession) -> dict:
    from ..models import PlatformSetting

    row = await session.get(PlatformSetting, KILL_SWITCH_KEY)
    if row is None:
        return {"engaged": False, "engaged_at": None, "engaged_by": None}
    return row.value or {"engaged": False, "engaged_at": None, "engaged_by": None}


async def set_kill_switch(session: AsyncSession, *, engaged: bool, actor: Optional[dict] = None) -> dict:
    from ..models import PlatformSetting

    value = (
        {
            "engaged": True,
            "engaged_at": utcnow().isoformat().replace("+00:00", "Z"),
            "engaged_by": actor,
        }
        if engaged
        else {"engaged": False, "engaged_at": None, "engaged_by": None}
    )
    row = await session.get(PlatformSetting, KILL_SWITCH_KEY)
    if row is None:
        row = PlatformSetting(key=KILL_SWITCH_KEY, value=value)
        session.add(row)
    else:
        row.value = value
    await session.flush()
    return value


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------
async def check_kill_switch(session: AsyncSession) -> Decision:
    state = await get_kill_switch(session)
    if state.get("engaged"):
        return Decision.block(
            "kill_switch_engaged",
            "The global kill switch is engaged. All autonomous activity is halted.",
        )
    return Decision.ok()


def check_campaign_live(campaign: Campaign) -> Decision:
    """A campaign only acts autonomously while live.

    Pausing retains every prospect and conversation; it only stops execution.
    """
    if campaign.status == "live":
        return Decision.ok()
    messages = {
        "draft": "This campaign is still a draft, so it cannot send outreach.",
        "paused": "This campaign is paused. No new outreach will fire until it is resumed.",
        "completed": "This campaign is completed and no longer sends outreach.",
        "archived": "This campaign is archived.",
    }
    return Decision.block(
        "campaign_not_live",
        messages.get(campaign.status, "This campaign is not live."),
        status=campaign.status,
    )


async def check_agent_enabled(session: AsyncSession, campaign_id: str, agent_key: str) -> Decision:
    config = (
        await session.execute(
            select(AgentConfig).where(
                AgentConfig.campaign_id == campaign_id, AgentConfig.agent_key == agent_key
            )
        )
    ).scalar_one_or_none()

    if config is None:
        # No explicit config means the agent runs with defaults.
        return Decision.ok()
    if not config.enabled:
        return Decision.block("agent_disabled", f"The {agent_key.replace('_', ' ')} agent is switched off for this campaign.")
    if config.state == "paused":
        return Decision.block("agent_paused", f"The {agent_key.replace('_', ' ')} agent is paused for this campaign.")
    return Decision.ok(config=config)


async def check_channel_open(session: AsyncSession, campaign_id: str, channel: str) -> Decision:
    row = (
        await session.execute(
            select(CampaignChannel).where(
                CampaignChannel.campaign_id == campaign_id, CampaignChannel.type == channel
            )
        )
    ).scalar_one_or_none()

    if row is None:
        return Decision.block("channel_not_configured", f"{channel.title()} is not configured for this campaign.")
    if row.state == "off":
        return Decision.block("channel_off", f"{channel.title()} is switched off for this campaign.")
    if row.state == "paused":
        return Decision.block("channel_paused", f"{channel.title()} is paused for this campaign.")
    return Decision.ok(channel_config=row)


async def check_suppression(session: AsyncSession, prospect: Prospect) -> Decision:
    """Global do-not-contact enforcement.

    Matching is done on every identifier we hold plus the company domain, so
    an opt-out at company level suppresses every contact there.
    """
    clauses = []
    if prospect.email:
        clauses.append(SuppressionEntry.email == prospect.email.lower())
    if prospect.phone:
        clauses.append(SuppressionEntry.phone == prospect.phone)
    if prospect.linkedin_url:
        clauses.append(SuppressionEntry.linkedin_url == prospect.linkedin_url)
    if prospect.company and prospect.company.domain:
        clauses.append(SuppressionEntry.domain == prospect.company.domain.lower())
    if not clauses:
        return Decision.ok()

    entry = (
        await session.execute(
            select(SuppressionEntry).where(
                or_(*clauses),
                or_(
                    SuppressionEntry.scope == "global",
                    SuppressionEntry.campaign_id == prospect.campaign_id,
                ),
            )
        )
    ).scalars().first()

    if entry:
        return Decision.block(
            "suppressed",
            f"This contact is on the do-not-contact list ({entry.reason}).",
            suppression_id=entry.id,
            suppression_reason=entry.reason,
        )
    return Decision.ok()


async def check_duplicate_outreach(session: AsyncSession, prospect: Prospect) -> Decision:
    """Campaign conflict handling.

    The same human can legitimately sit in two campaigns, but they must not be
    contacted by both. The first campaign to make contact takes ownership; any
    other campaign is blocked and the conflict is surfaced for a human to
    resolve rather than silently dropped.
    """
    if not prospect.identity_key:
        return Decision.ok()

    conflict = (
        await session.execute(
            select(Prospect)
            .where(
                Prospect.identity_key == prospect.identity_key,
                Prospect.id != prospect.id,
                Prospect.campaign_id != prospect.campaign_id,
                Prospect.last_contacted_at.is_not(None),
            )
            .order_by(Prospect.last_contacted_at.asc())
            .limit(1)
        )
    ).scalars().first()

    if conflict is None:
        return Decision.ok()

    campaign_name = (await session.get(Campaign, conflict.campaign_id)).name
    return Decision.block(
        "duplicate_outreach",
        f"This prospect is already in active outreach under {campaign_name}.",
        conflicting_campaign_id=conflict.campaign_id,
        conflicting_campaign_name=campaign_name,
        conflicting_prospect_id=conflict.id,
    )


def check_contact_frequency(prospect: Prospect, campaign: Campaign) -> Decision:
    """Excessive contact frequency guard: respect the configured gap between
    touches and the maximum number of follow-ups."""
    policies = campaign.policies or {}
    max_follow_ups = int(policies.get("max_follow_ups", 3))
    gap_hours = int(policies.get("follow_up_gap_hours", 72))

    if prospect.follow_up_count >= max_follow_ups:
        return Decision.block(
            "follow_up_limit",
            f"This prospect has already had {prospect.follow_up_count} follow-ups, the campaign limit.",
        )

    if prospect.last_contacted_at:
        last = prospect.last_contacted_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=dt.timezone.utc)
        earliest = last + dt.timedelta(hours=gap_hours)
        if earliest > utcnow():
            return Decision(
                allowed=False,
                code="too_soon",
                reason=f"Last contacted less than {gap_hours} hours ago.",
                retry_after=earliest,
            )
    return Decision.ok()


def check_working_hours(campaign: Campaign, rep_hours: Optional[dict] = None, now: Optional[dt.datetime] = None) -> Decision:
    """Outreach only fires inside the sending rep's working hours.

    Sending a 3am email is the fastest way to look like a bot, so this is a
    quality guard as much as a compliance one.
    """
    policies = campaign.policies or {}
    if not policies.get("working_hours_only", True):
        return Decision.ok()

    hours = rep_hours or {"start": 9, "end": 19, "days": [0, 1, 2, 3, 4], "offset_hours": 5.5}
    now = now or utcnow()
    offset = float(hours.get("offset_hours", 5.5))
    local = now + dt.timedelta(hours=offset)

    if local.weekday() not in hours.get("days", [0, 1, 2, 3, 4]):
        return Decision(
            allowed=False,
            code="outside_working_days",
            reason="Outside the sending rep's working days.",
            retry_after=(local + dt.timedelta(days=1)).replace(hour=int(hours.get("start", 9)), minute=0) - dt.timedelta(hours=offset),
        )

    start, end = int(hours.get("start", 9)), int(hours.get("end", 19))
    if not (start <= local.hour < end):
        target = local.replace(hour=start, minute=0, second=0, microsecond=0)
        if local.hour >= end:
            target += dt.timedelta(days=1)
        return Decision(
            allowed=False,
            code="outside_working_hours",
            reason=f"Outside the sending rep's working hours ({start}:00-{end}:00).",
            retry_after=target - dt.timedelta(hours=offset),
        )
    return Decision.ok()


def _window_start(now: Optional[dt.datetime] = None) -> dt.datetime:
    now = now or utcnow()
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


async def check_rate_limit(
    session: AsyncSession, *, scope: str, scope_key: str, limit: int
) -> Decision:
    """Fixed daily window counters.

    Read-only here; ``consume_rate_limit`` increments only once a send has
    actually been authorised, so a blocked action never burns quota.
    """
    window = _window_start()
    row = (
        await session.execute(
            select(RateLimitCounter).where(
                RateLimitCounter.scope == scope,
                RateLimitCounter.scope_key == scope_key,
                RateLimitCounter.window_start == window,
            )
        )
    ).scalar_one_or_none()

    used = row.count if row else 0
    if used >= limit:
        return Decision(
            allowed=False,
            code="rate_limited",
            reason=f"Daily limit of {limit} reached for {scope.replace('_', ' ')}.",
            retry_after=window + dt.timedelta(days=1),
            context={"used": used, "limit": limit},
        )
    return Decision.ok(used=used, limit=limit)


async def consume_rate_limit(session: AsyncSession, *, scope: str, scope_key: str) -> None:
    window = _window_start()
    row = (
        await session.execute(
            select(RateLimitCounter).where(
                RateLimitCounter.scope == scope,
                RateLimitCounter.scope_key == scope_key,
                RateLimitCounter.window_start == window,
            )
        )
    ).scalar_one_or_none()

    if row is None:
        session.add(
            RateLimitCounter(
                id=new_id("rl"), scope=scope, scope_key=scope_key, window_start=window, count=1
            )
        )
    else:
        row.count += 1
    await session.flush()


# ---------------------------------------------------------------------------
# Composite gates
# ---------------------------------------------------------------------------
async def can_agent_run(
    session: AsyncSession, campaign: Campaign, agent_key: str
) -> Decision:
    """Gate for any agent invocation (reasoning, no external side effect).

    Even pure reasoning is gated, because running models on a paused campaign
    burns money for output nobody asked for.
    """
    for check in (
        await check_kill_switch(session),
        check_campaign_live(campaign),
        await check_agent_enabled(session, campaign.id, agent_key),
    ):
        if not check.allowed:
            return check
    return Decision.ok()


async def can_send(
    session: AsyncSession,
    *,
    campaign: Campaign,
    prospect: Prospect,
    channel: str,
    rep_hours: Optional[dict] = None,
    skip_frequency: bool = False,
) -> Decision:
    """The full gate for an outbound message on a channel.

    Every check that follows is ANDed; the first block wins and is returned
    with the reason a manager will see. This function is the only sanctioned
    way to authorise external contact.
    """
    checks: Sequence[Decision] = (
        await check_kill_switch(session),
        check_campaign_live(campaign),
        await check_channel_open(session, campaign.id, channel),
        await check_suppression(session, prospect),
        await check_duplicate_outreach(session, prospect),
        check_working_hours(campaign, rep_hours),
    )
    for decision in checks:
        if not decision.allowed:
            return decision

    if not skip_frequency:
        freq = check_contact_frequency(prospect, campaign)
        if not freq.allowed:
            return freq

    channel_row = (
        await session.execute(
            select(CampaignChannel).where(
                CampaignChannel.campaign_id == campaign.id, CampaignChannel.type == channel
            )
        )
    ).scalar_one_or_none()
    channel_limit = channel_row.daily_limit if channel_row else settings.DEFAULT_DAILY_SEND_LIMIT

    channel_rate = await check_rate_limit(
        session,
        scope="campaign_channel",
        scope_key=f"{campaign.id}:{channel}",
        limit=channel_limit,
    )
    if not channel_rate.allowed:
        return channel_rate

    campaign_limit = int((campaign.policies or {}).get("daily_send_limit", settings.DEFAULT_DAILY_SEND_LIMIT))
    campaign_rate = await check_rate_limit(
        session, scope="campaign", scope_key=campaign.id, limit=campaign_limit
    )
    if not campaign_rate.allowed:
        return campaign_rate

    return Decision.ok(channel=channel, channel_limit=channel_limit)


async def count_sent_today(session: AsyncSession, campaign_id: str) -> int:
    window = _window_start()
    return int(
        (
            await session.execute(
                select(func.count(Message.id)).where(
                    Message.campaign_id == campaign_id,
                    Message.direction == "outbound",
                    Message.status.in_(["sent", "delivered", "replied"]),
                    Message.sent_at >= window,
                )
            )
        ).scalar()
        or 0
    )
