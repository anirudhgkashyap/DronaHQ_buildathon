"""Prompt management: versioning, activation, rollback and diff.

Prompts are the program. Treating them as an editable text field would mean
nobody could answer "what was this agent told when it wrote that email?", so
they are versioned exactly like code:

* **Versions are immutable.** Editing creates version N+1; nothing ever
  rewrites an existing row. ``AgentRun.prompt_version_id`` therefore stays
  meaningful forever, and the audit trail keeps pointing at the real text.
* **Exactly one version is active** per (campaign, agent). Activating one
  deactivates its siblings in the same transaction.
* **Rollback is not a special operation.** It is activating an older version —
  the same endpoint, the same audit row. There is no separate revert path to
  get wrong under pressure, and the version that was rolled back to is still
  the same object it always was.
* **Scope is the campaign.** There is no global prompt, so tuning one
  campaign's personalisation agent cannot change another's behaviour.
"""
from __future__ import annotations

import difflib
from typing import Annotated, Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update

from .. import audit
from ..agents.schemas import AGENT_LABELS
from ..db import new_id
from ..models import AGENT_KEYS, PromptVersion, utcnow
from ..serializers import iso, serialize_prompt_version
from .deps import SessionDep, UserDep, actor, load_campaign, require_choice
from .errors import not_found

router = APIRouter(tags=["prompts"])

CAMPAIGN_PROMPT_KEY = "__campaign__"
PROMPT_KEYS = [CAMPAIGN_PROMPT_KEY, *AGENT_KEYS]
PROMPT_LABELS = {CAMPAIGN_PROMPT_KEY: "Campaign System Prompt", **AGENT_LABELS}


class PromptCreate(BaseModel):
    content: str = Field(min_length=1, max_length=50_000)
    notes: Optional[str] = Field(default=None, max_length=2000)
    #: Create-and-activate in one call. Off by default so a draft can be
    #: reviewed before it starts writing to real prospects.
    activate: bool = False


@router.get("/campaigns/{campaign_id}/prompts")
async def list_prompt_keys(campaign_id: str, session: SessionDep, _: UserDep) -> dict:
    """Every prompt slot for this campaign, with its active version.

    Two grouped queries cover all seven slots — one for the counts, one for the
    active rows — rather than a query per agent.
    """
    await load_campaign(session, campaign_id)

    counts = {
        agent_key: int(count or 0)
        for agent_key, count in (
            await session.execute(
                select(PromptVersion.agent_key, func.count(PromptVersion.id))
                .where(PromptVersion.campaign_id == campaign_id)
                .group_by(PromptVersion.agent_key)
            )
        )
    }
    active = {
        version.agent_key: version
        for version in (
            await session.execute(
                select(PromptVersion).where(
                    PromptVersion.campaign_id == campaign_id,
                    PromptVersion.is_active.is_(True),
                )
            )
        ).scalars()
    }

    return {
        "items": [
            {
                "agent_key": agent_key,
                "label": PROMPT_LABELS.get(agent_key, agent_key),
                "version_count": counts.get(agent_key, 0),
                "active_version": (
                    serialize_prompt_version(active[agent_key]) if agent_key in active else None
                ),
            }
            for agent_key in PROMPT_KEYS
        ]
    }


@router.get("/campaigns/{campaign_id}/prompts/{agent_key}")
async def list_versions(
    campaign_id: str, agent_key: str, session: SessionDep, _: UserDep
) -> dict:
    """Full version history, newest first — the rollback menu."""
    await load_campaign(session, campaign_id)
    require_choice(agent_key, PROMPT_KEYS, "prompt")

    versions = (
        await session.execute(
            select(PromptVersion)
            .where(PromptVersion.campaign_id == campaign_id, PromptVersion.agent_key == agent_key)
            .order_by(PromptVersion.version.desc())
        )
    ).scalars().all()

    return {
        "agent_key": agent_key,
        "label": PROMPT_LABELS.get(agent_key, agent_key),
        "versions": [serialize_prompt_version(v) for v in versions],
    }


@router.post("/campaigns/{campaign_id}/prompts/{agent_key}", status_code=201)
async def create_version(
    campaign_id: str,
    agent_key: str,
    body: PromptCreate,
    session: SessionDep,
    user: UserDep,
) -> dict:
    """Create a new immutable version.

    Never mutates an existing row — that is the whole point. The new version is
    ``max(version) + 1`` and is inactive unless ``activate`` is set, so writing
    a prompt and putting it in front of prospects stay two separate decisions.
    """
    campaign = await load_campaign(session, campaign_id)
    require_choice(agent_key, PROMPT_KEYS, "prompt")

    highest = int(
        (
            await session.execute(
                select(func.max(PromptVersion.version)).where(
                    PromptVersion.campaign_id == campaign_id,
                    PromptVersion.agent_key == agent_key,
                )
            )
        ).scalar()
        or 0
    )

    version = PromptVersion(
        id=new_id("pv"),
        campaign_id=campaign_id,
        agent_key=agent_key,
        version=highest + 1,
        content=body.content,
        notes=body.notes,
        is_active=False,
        created_by_id=user.id,
    )
    version.created_by = user
    session.add(version)
    await session.flush()

    label = PROMPT_LABELS.get(agent_key, agent_key)
    if body.activate:
        await _activate(session, campaign_id, agent_key, version)

    await audit.record_campaign_event(
        session,
        campaign,
        event_type="prompt_version_created",
        severity="info",
        message=(
            f"prompt v{version.version} created for the {label} by {actor(user)}"
            + (" and activated." if body.activate else ".")
        ),
        actor=actor(user),
        payload={"agent_key": agent_key, "version": version.version, "activated": body.activate},
    )
    await session.commit()
    return serialize_prompt_version(version)


async def _activate(session, campaign_id: str, agent_key: str, version: PromptVersion) -> None:
    """Flip the active flag, atomically, for one (campaign, agent) pair.

    A bulk UPDATE rather than a loop so there is never a moment where two
    versions are active — an agent resolving its prompt mid-switch must get
    exactly one answer.
    """
    await session.execute(
        update(PromptVersion)
        .where(
            PromptVersion.campaign_id == campaign_id,
            PromptVersion.agent_key == agent_key,
            PromptVersion.id != version.id,
            PromptVersion.is_active.is_(True),
        )
        .values(is_active=False)
    )
    version.is_active = True
    version.activated_at = utcnow()
    await session.flush()


@router.post("/campaigns/{campaign_id}/prompts/{agent_key}/{version}/activate")
async def activate_version(
    campaign_id: str, agent_key: str, version: int, session: SessionDep, user: UserDep
) -> dict:
    """Make this version the one the agent runs.

    **Rollback is this endpoint.** Activating version 4 after version 6 turned
    out badly is a rollback; there is no separate revert call, because a
    rollback that takes a different code path is a rollback that is only tested
    when it is already too late. The old version is still there, unchanged, and
    re-activating the newer one later is the same single call.
    """
    campaign = await load_campaign(session, campaign_id)
    require_choice(agent_key, PROMPT_KEYS, "prompt")

    target = (
        await session.execute(
            select(PromptVersion).where(
                PromptVersion.campaign_id == campaign_id,
                PromptVersion.agent_key == agent_key,
                PromptVersion.version == version,
            )
        )
    ).scalar_one_or_none()
    if target is None:
        raise not_found(f"v{version} of that prompt", agent_key)

    previous = (
        await session.execute(
            select(PromptVersion.version).where(
                PromptVersion.campaign_id == campaign_id,
                PromptVersion.agent_key == agent_key,
                PromptVersion.is_active.is_(True),
            )
        )
    ).scalars().first()

    await _activate(session, campaign_id, agent_key, target)

    label = PROMPT_LABELS.get(agent_key, agent_key)
    rolled_back = previous is not None and previous > version
    await audit.record_campaign_event(
        session,
        campaign,
        event_type="prompt_version_activated",
        severity="warning" if rolled_back else "success",
        message=(
            f"prompt v{version} activated for the {label} by {actor(user)}"
            + (f", rolling back from v{previous}." if rolled_back else ".")
        ),
        actor=actor(user),
        payload={
            "agent_key": agent_key,
            "version": version,
            "previous_version": previous,
            "rollback": rolled_back,
        },
    )
    await session.commit()
    return serialize_prompt_version(target)


@router.get("/campaigns/{campaign_id}/prompts/{agent_key}/diff")
async def diff_versions(
    campaign_id: str,
    agent_key: str,
    session: SessionDep,
    _: UserDep,
    a: Annotated[int, Query(ge=1)],
    b: Annotated[int, Query(ge=1)],
) -> dict:
    """Unified diff between two versions.

    "The reply rate dropped after Tuesday" is only answerable if you can see
    what changed on Tuesday, so this is the harness endpoint that turns a
    prompt experiment into a finding.
    """
    await load_campaign(session, campaign_id)
    require_choice(agent_key, PROMPT_KEYS, "prompt")

    versions = {
        version.version: version
        for version in (
            await session.execute(
                select(PromptVersion).where(
                    PromptVersion.campaign_id == campaign_id,
                    PromptVersion.agent_key == agent_key,
                    PromptVersion.version.in_([a, b]),
                )
            )
        ).scalars()
    }
    for wanted in (a, b):
        if wanted not in versions:
            raise not_found(f"v{wanted} of that prompt", agent_key)

    left, right = versions[a], versions[b]
    lines = list(
        difflib.unified_diff(
            left.content.splitlines(),
            right.content.splitlines(),
            fromfile=f"v{a}",
            tofile=f"v{b}",
            lineterm="",
        )
    )
    # "+++"/"---" are the file headers, not content changes; counting them
    # would report every diff as one line bigger than it is.
    added = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))

    return {
        "a": a,
        "b": b,
        "diff": "\n".join(lines),
        "added": added,
        "removed": removed,
        "a_created_at": iso(left.created_at),
        "b_created_at": iso(right.created_at),
    }



# ---------------------------------------------------------------------------
# Global prompt library (the settings page's Prompts tab)
# ---------------------------------------------------------------------------
# Prompts are scoped to a campaign in the documented API (above): tuning one
# campaign's agent cannot change another's behaviour. The settings page,
# though, wants one flat library across every campaign to browse and activate
# from — so this lists every agent-level version (the campaign system prompt
# slot is excluded; it isn't a per-agent behaviour) across all campaigns, and
# activation resolves the version by id rather than by (campaign, agent,
# version) since that's what a flat list can address.
@router.get("/prompts")
async def list_all_prompts(session: SessionDep, _: UserDep, limit: Annotated[int, Query(ge=1, le=500)] = 100) -> dict:
    versions = (
        await session.execute(
            select(PromptVersion)
            .where(PromptVersion.agent_key != CAMPAIGN_PROMPT_KEY)
            .order_by(PromptVersion.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return {
        "items": [
            {
                "id": v.id,
                "campaign_id": v.campaign_id,
                "agent": v.agent_key,
                "version": v.version,
                "active": v.is_active,
                "created_at": iso(v.created_at),
                "preview": v.content[:200],
            }
            for v in versions
        ]
    }


@router.post("/prompts/{version_id}/activate")
async def activate_prompt_by_id(version_id: str, session: SessionDep, user: UserDep) -> dict:
    """Activate a version addressed directly by id — the flat library's
    equivalent of the campaign-scoped ``.../prompts/{agent_key}/{version}/activate``."""
    target = await session.get(PromptVersion, version_id)
    if target is None:
        raise not_found("prompt version", version_id)

    await _activate(session, target.campaign_id, target.agent_key, target)

    label = PROMPT_LABELS.get(target.agent_key, target.agent_key)
    await audit.record(
        session,
        entity_type="prompt_version",
        entity_id=target.id,
        event_type="prompt_version_activated",
        severity="success",
        message=f"prompt v{target.version} activated for the {label} by {actor(user)}.",
        campaign_id=target.campaign_id,
        actor=actor(user),
        payload={"agent_key": target.agent_key, "version": target.version},
    )
    await session.commit()
    return serialize_prompt_version(target)


__all__ = ["router", "CAMPAIGN_PROMPT_KEY", "PROMPT_KEYS"]
