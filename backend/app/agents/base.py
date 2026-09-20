"""The agent runner: one function every agent invocation goes through.

``run_agent`` is where the platform's non-negotiables are applied uniformly,
so no individual agent can forget one:

* the guardrail gate (kill switch, campaign status, agent pause),
* idempotency (a repeated key returns the stored result, never re-executes),
* prompt resolution from the campaign's *active* version, recorded on the run,
* RAG retrieval before any decision that reaches a customer,
* cost, latency and token accounting,
* structured-output validation with a bounded retry on malformed output,
* an append-only audit entry.

Agents themselves (icp_fit.py and friends) only assemble variables and read
the result. That separation is what keeps six agents from drifting into six
different reliability stories.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import time
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit
from ..config import settings
from ..db import new_id
from ..models import AgentConfig, AgentRun, Campaign, Prospect, PromptVersion, utcnow
from ..orchestrator.guardrails import Decision, can_agent_run
from ..rag import store as rag_store
from .client import (
    AgentError,
    AgentOutputError,
    AgentResult,
    AgentTransportError,
    get_agent_client,
)
from .schemas import AGENT_LABELS, schema_for

logger = logging.getLogger(__name__)

# Mechanical steps do not need the strongest model. Routing them down is the
# single biggest lever on cost per prospect, so it is a default, not an option.
CHEAP_MODEL = "claude-haiku-4-5"
CHEAP_AGENTS = {"icp_fit", "research_enrichment"}


class AgentSkipped(Exception):
    """Raised when a guardrail refused the run. Carries the decision so the
    caller can record why without re-deriving it."""

    def __init__(self, decision: Decision) -> None:
        super().__init__(decision.reason)
        self.decision = decision


# ---------------------------------------------------------------------------
# Prompt resolution
# ---------------------------------------------------------------------------
async def active_prompt(
    session: AsyncSession, campaign_id: str, agent_key: str
) -> Optional[PromptVersion]:
    """The prompt version currently live for this (campaign, agent).

    Campaign-scoped by construction: there is no global prompt an edit could
    leak through, which is what keeps one campaign's changes out of another's
    behaviour.
    """
    return (
        await session.execute(
            select(PromptVersion).where(
                PromptVersion.campaign_id == campaign_id,
                PromptVersion.agent_key == agent_key,
                PromptVersion.is_active.is_(True),
            )
        )
    ).scalars().first()


def render_prompt(template: str, variables: dict) -> str:
    """Fill ``{{name}}`` placeholders.

    Deliberately not a real template engine: prompts are operator-editable, and
    an operator typing a stray brace should produce a slightly odd prompt, not
    a server error or an injection point.
    """
    rendered = template
    for key, value in variables.items():
        if isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
        elif value is None:
            text = "not available"
        else:
            text = str(value)
        rendered = rendered.replace("{{" + key + "}}", text)
    return rendered


def _model_for(agent_key: str, config: Optional[AgentConfig]) -> str:
    if config and config.model:
        return config.model
    return CHEAP_MODEL if agent_key in CHEAP_AGENTS else "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------
async def retrieve_context(
    session: AsyncSession,
    *,
    campaign_id: str,
    query: str,
    doc_types: Optional[list[str]] = None,
    limit: int = 5,
) -> tuple[str, list[str]]:
    """Fetch campaign + platform knowledge for a query.

    Returns the text block to drop into the prompt and the chunk ids, which get
    recorded on the run so a judge can see exactly what the agent was shown.
    """
    try:
        hits = await rag_store.search(
            session, query, campaign_id=campaign_id, doc_types=doc_types, limit=limit
        )
    except Exception:  # retrieval must never take down the pipeline
        logger.exception("retrieval failed for campaign %s", campaign_id)
        return "", []

    if not hits:
        return "", []

    blocks = []
    ids = []
    for chunk, score in hits:
        ids.append(chunk.id)
        source = (chunk.meta or {}).get("source") or chunk.doc_type
        blocks.append(f"[{chunk.doc_type}] {chunk.title} (source: {source}, relevance {score:.2f})\n{chunk.content}")
    return "\n\n---\n\n".join(blocks), ids


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------
async def run_agent(
    session: AsyncSession,
    *,
    agent_key: str,
    campaign: Campaign,
    idempotency_key: str,
    variables: dict,
    prospect: Optional[Prospect] = None,
    retrieval_query: Optional[str] = None,
    retrieval_types: Optional[list[str]] = None,
    retrieval_limit: int = 5,
    enforce_guardrails: bool = True,
    actor: str = "system",
) -> AgentRun:
    """Run one agent and persist the attempt.

    Always returns an ``AgentRun``; inspect ``.status`` and ``.output`` rather
    than expecting an exception. The only exception raised is ``AgentSkipped``,
    and only when a guardrail refused before any work happened.
    """
    # --- Idempotency: a completed run for this key is the answer. -----------
    existing = (
        await session.execute(select(AgentRun).where(AgentRun.idempotency_key == idempotency_key))
    ).scalar_one_or_none()
    if existing is not None and existing.status in {"succeeded", "skipped"}:
        logger.debug("idempotent hit for %s (%s)", agent_key, idempotency_key)
        return existing

    # --- Guardrails before any spend. ---------------------------------------
    if enforce_guardrails:
        decision = await can_agent_run(session, campaign, agent_key)
        if not decision.allowed:
            run = existing or AgentRun(id=new_id("run"), idempotency_key=idempotency_key)
            run.campaign_id = campaign.id
            run.prospect_id = prospect.id if prospect else None
            run.agent_key = agent_key
            run.status = "skipped"
            run.input = variables
            run.error = f"{decision.code}: {decision.reason}"
            run.finished_at = utcnow()
            if existing is None:
                session.add(run)
            await session.flush()
            raise AgentSkipped(decision)

    config = (
        await session.execute(
            select(AgentConfig).where(
                AgentConfig.campaign_id == campaign.id, AgentConfig.agent_key == agent_key
            )
        )
    ).scalar_one_or_none()

    prompt_version = await active_prompt(session, campaign.id, agent_key)
    from .schemas import default_prompt

    template = prompt_version.content if prompt_version else default_prompt(agent_key)

    # --- Retrieval ----------------------------------------------------------
    retrieved_text, chunk_ids = "", []
    if retrieval_query:
        retrieved_text, chunk_ids = await retrieve_context(
            session,
            campaign_id=campaign.id,
            query=retrieval_query,
            doc_types=retrieval_types,
            limit=retrieval_limit,
        )

    full_variables = {
        "campaign_name": campaign.name,
        "objective": campaign.objective or "",
        "icp": campaign.icp,
        "product_context": campaign.product_context,
        "retrieved_knowledge": retrieved_text or "No relevant knowledge retrieved.",
        **variables,
    }
    prompt = render_prompt(template, full_variables)
    model = _model_for(agent_key, config)
    schema = schema_for(agent_key)

    run = existing or AgentRun(id=new_id("run"), idempotency_key=idempotency_key)
    run.campaign_id = campaign.id
    run.prospect_id = prospect.id if prospect else None
    run.agent_key = agent_key
    run.status = "running"
    run.input = {k: v for k, v in full_variables.items() if k != "retrieved_knowledge"}
    run.prompt_version_id = prompt_version.id if prompt_version else None
    run.retrieved_chunk_ids = chunk_ids
    run.model = model
    run.started_at = utcnow()
    if existing is None:
        session.add(run)
    await session.flush()

    client = get_agent_client()
    started = time.perf_counter()

    result: Optional[AgentResult] = None
    last_error: Optional[Exception] = None

    # One retry on malformed output: models occasionally produce unparseable
    # JSON, and a second attempt is far cheaper than dropping the prospect.
    for attempt in range(2):
        try:
            result = await client.invoke(
                agent_key=agent_key,
                prompt=prompt,
                variables=full_variables,
                output_schema=schema,
                model=model,
                dronahq_agent_id=config.dronahq_agent_id if config else None,
            )
            break
        except AgentOutputError as exc:
            last_error = exc
            logger.warning("malformed output from %s (attempt %s): %s", agent_key, attempt + 1, exc)
            prompt = (
                prompt
                + "\n\nYour previous response could not be parsed as JSON. "
                "Respond with valid JSON only, no prose and no code fences."
            )
        except AgentTransportError as exc:
            last_error = exc
            logger.warning("transport failure for %s: %s", agent_key, exc)
            break
        except AgentError as exc:
            last_error = exc
            break

    latency_ms = int((time.perf_counter() - started) * 1000)

    if result is None:
        run.status = "failed"
        run.error = str(last_error) if last_error else "unknown agent failure"
        run.latency_ms = latency_ms
        run.finished_at = utcnow()
        await session.flush()
        await audit.record(
            session,
            entity_type="agent_run",
            entity_id=run.id,
            event_type="agent_failed",
            severity="error",
            message=f"{AGENT_LABELS.get(agent_key, agent_key)} failed: {run.error}",
            campaign_id=campaign.id,
            campaign_name=campaign.name,
            actor=actor,
            payload={"agent_key": agent_key, "prospect_id": run.prospect_id},
            prompt_version_id=run.prompt_version_id,
        )
        return run

    for field_name, value in result.as_run_fields().items():
        setattr(run, field_name, value)
    run.latency_ms = latency_ms
    run.status = "succeeded"
    run.finished_at = utcnow()
    await session.flush()

    await audit.record(
        session,
        entity_type="agent_run",
        entity_id=run.id,
        event_type="agent_completed",
        severity="info",
        message=f"{AGENT_LABELS.get(agent_key, agent_key)} completed",
        campaign_id=campaign.id,
        campaign_name=campaign.name,
        actor=actor,
        payload={
            "agent_key": agent_key,
            "prospect_id": run.prospect_id,
            "executor": result.executor,
            "model": result.model,
            "cost_usd": result.cost_usd,
            "latency_ms": latency_ms,
            "repaired": result.repaired,
            "retrieved_chunks": len(chunk_ids),
        },
        prompt_version_id=run.prompt_version_id,
    )
    return run


def make_key(*parts: Any) -> str:
    """Deterministic idempotency key.

    Built from stable identifiers only (never a timestamp), so the same logical
    step retried after a crash resolves to the same key and is not re-executed.
    """
    return ":".join(str(p) for p in parts if p is not None)
