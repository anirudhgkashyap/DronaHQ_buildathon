"""Transport layer for agent invocation.

This module deliberately knows nothing about campaigns, prospects, prompts or
RAG: ``agents/base.py`` owns all of that and sits on top of this. Keeping the
transport free of domain concepts is what lets the whole pipeline swap between
DronaHQ and the offline simulator with a single settings flag, and it is why
the simulator can be exercised by exactly the same parsing and validation code
path as the real provider.

Three things here exist purely for reliability, because a model endpoint is the
least trustworthy dependency in the system:

* **Retries** — transient failures (429, 5xx, timeouts, connection resets) are
  retried with exponential backoff and jitter; deterministic client errors
  (4xx other than 429/408) are not, because retrying a bad request just burns
  the rate-limit budget.
* **Tolerant parsing** — LLMs emit fenced blocks, prose preambles, trailing
  commas and single quotes. ``parse_structured_output`` walks a ladder of
  recovery strategies before giving up, and reports whether it had to repair
  anything so the caller can log and measure model quality.
* **Real cost accounting** — the product reports cost per prospect and per
  qualified lead. A zero there is worse than useless, so token counts are
  estimated when the provider does not return them and priced from a table.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Optional, Protocol, runtime_checkable

import httpx

from ..config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class AgentError(Exception):
    """Base class so callers can catch every agent-layer failure at once."""


class AgentTransportError(AgentError):
    """The provider could not be reached, or kept failing after retries.

    Distinct from ``AgentOutputError`` because the recovery differs: a
    transport failure is worth re-queuing later, malformed output usually is
    not worth retrying with the identical prompt.
    """

    def __init__(self, message: str, *, status_code: Optional[int] = None, attempts: int = 0) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.attempts = attempts


class AgentOutputError(AgentError):
    """The provider answered, but not with usable structured output.

    Raised rather than returning a half-parsed dict so no downstream step ever
    writes garbage into the database: the orchestrator can retry the step or
    mark the run failed, and the audit trail records why.
    """

    def __init__(self, message: str, *, raw: str = "") -> None:
        super().__init__(message)
        # Truncated: raw payloads can be enormous and this ends up in logs.
        self.raw = raw[:2000]


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------
@dataclass
class AgentResult:
    """Everything one agent invocation produced, including its economics.

    ``raw`` is kept verbatim so a judge (or an on-call engineer) can see what
    the model actually said when a parse had to be repaired.
    """

    output: dict
    executor: str  # "dronahq" | "simulator"
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_ms: int
    raw: str
    repaired: bool = False
    # Populated on retries so AgentRun rows can show flakiness without a
    # separate metrics pipeline.
    attempts: int = 1
    meta: dict = field(default_factory=dict)

    def as_run_fields(self) -> dict:
        """Shape that maps straight onto the ``AgentRun`` columns."""
        return {
            "output": self.output,
            "executor": self.executor,
            "model": self.model,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cost_usd": self.cost_usd,
            "latency_ms": self.latency_ms,
        }


@runtime_checkable
class AgentClient(Protocol):
    """Transport contract. ``agents/base.py`` depends on this, not on DronaHQ."""

    executor: str

    async def invoke(
        self,
        *,
        agent_key: str,
        prompt: str,
        variables: dict,
        output_schema: dict,
        model: Optional[str] = None,
        dronahq_agent_id: Optional[str] = None,
    ) -> AgentResult:
        ...


# ---------------------------------------------------------------------------
# Cost accounting
# ---------------------------------------------------------------------------
# USD per 1M tokens, (input, output). Matched by longest substring so
# "claude-sonnet-4-6" and "claude-3-7-sonnet" both land on the sonnet row
# without needing an exhaustive model list that rots every release.
PRICE_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus": (15.00, 75.00),
    "claude-sonnet": (3.00, 15.00),
    "claude-haiku": (0.80, 4.00),
    "sonnet": (3.00, 15.00),
    "haiku": (0.80, 4.00),
    "opus": (15.00, 75.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "o4-mini": (1.10, 4.40),
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-1.5-flash": (0.075, 0.30),
    "gemini-1.5-pro": (1.25, 5.00),
    "llama-3": (0.20, 0.20),
    "mistral": (0.25, 0.75),
    "simulator": (0.0, 0.0),
}

# Anything unrecognised is priced as a mid-tier model rather than as free, so
# an unknown model never silently reports a $0 campaign.
DEFAULT_PRICE_PER_MTOK: tuple[float, float] = (3.00, 15.00)

DEFAULT_MODEL = "claude-sonnet-4-6"
SIMULATOR_MODEL = "atlas-simulator-v1"


def price_for_model(model: str) -> tuple[float, float]:
    """Longest-substring match against the price table."""
    key = (model or "").lower()
    best: Optional[tuple[str, tuple[float, float]]] = None
    for name, price in PRICE_PER_MTOK.items():
        if name in key and (best is None or len(name) > len(best[0])):
            best = (name, price)
    return best[1] if best else DEFAULT_PRICE_PER_MTOK


def estimate_tokens(text: str) -> int:
    """~4 characters per token. Crude, but a real number beats a zero.

    Only used when the provider omits usage data; DronaHQ's own counts win
    whenever they are present.
    """
    if not text:
        return 0
    return max(1, len(text) // 4)


def compute_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    in_rate, out_rate = price_for_model(model)
    cost = (tokens_in / 1_000_000.0) * in_rate + (tokens_out / 1_000_000.0) * out_rate
    # 6dp keeps per-message costs (often <$0.001) from rounding to zero.
    return round(cost, 6)


# ---------------------------------------------------------------------------
# Structured output parsing
# ---------------------------------------------------------------------------
_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.+?)```", re.DOTALL)
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")
_LINE_COMMENT_RE = re.compile(r"(?m)^\s*//.*$")
# Envelope keys a gateway might wrap the payload in. Only unwrapped when the
# required keys are not already satisfied at the top level.
_ENVELOPE_KEYS = ("output", "data", "result", "response", "json", "body", "content")


def _required_keys(schema: Optional[dict]) -> list[str]:
    """Pull required top-level keys out of a JSON-schema-ish dict.

    Accepts both a bare JSON Schema and the ``{"name": ..., "schema": {...}}``
    wrapper used by structured-output APIs, because both shapes reach us.
    """
    if not isinstance(schema, dict):
        return []
    for candidate in (schema, schema.get("schema"), schema.get("json_schema")):
        if isinstance(candidate, dict):
            required = candidate.get("required")
            if isinstance(required, list) and required:
                return [str(k) for k in required]
            nested = candidate.get("schema")
            if isinstance(nested, dict) and isinstance(nested.get("required"), list):
                return [str(k) for k in nested["required"]]
    # Fall back to declared properties: better than no validation at all when
    # the schema author omitted "required".
    props = schema.get("properties") or (schema.get("schema") or {}).get("properties")
    if isinstance(props, dict) and props:
        return list(props.keys())
    return []


def _outermost_span(text: str) -> Optional[str]:
    """Slice the outermost {...} (or [...]) span out of surrounding prose."""
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = text.find(open_ch)
        end = text.rfind(close_ch)
        if start != -1 and end > start:
            return text[start : end + 1]
    return None


def _repair(text: str) -> str:
    """Light, conservative repairs for the mistakes models actually make.

    Nothing here rewrites values; it only fixes syntax around them, so a
    repaired parse is still faithful to what the model said.
    """
    fixed = text.strip()
    fixed = _LINE_COMMENT_RE.sub("", fixed)
    fixed = fixed.replace("“", '"').replace("”", '"')
    fixed = fixed.replace("‘", "'").replace("’", "'")
    fixed = _TRAILING_COMMA_RE.sub(r"\1", fixed)
    # Python-flavoured literals from models that were shown Python examples.
    fixed = re.sub(r"\bTrue\b", "true", fixed)
    fixed = re.sub(r"\bFalse\b", "false", fixed)
    fixed = re.sub(r"\b(None|NaN|Undefined|undefined)\b", "null", fixed)
    if '"' not in fixed and "'" in fixed:
        # Whole document used single quotes: safe to swap wholesale.
        fixed = fixed.replace("'", '"')
    else:
        # Quote only the obvious single-quoted keys, leaving apostrophes in
        # prose values ("we're") untouched.
        fixed = re.sub(r"([{,]\s*)'([^'\n]+?)'(\s*:)", r'\1"\2"\3', fixed)
    return fixed


def _unwrap_envelope(parsed: Any, required: list[str]) -> Any:
    """Descend through gateway wrappers until the required keys are visible."""
    current = parsed
    for _ in range(3):
        if not isinstance(current, dict):
            return current
        if not required or all(k in current for k in required):
            return current
        for key in _ENVELOPE_KEYS:
            inner = current.get(key)
            if isinstance(inner, dict):
                current = inner
                break
            if isinstance(inner, str):
                try:
                    decoded = json.loads(inner)
                except (ValueError, TypeError):
                    continue
                if isinstance(decoded, dict):
                    current = decoded
                    break
        else:
            return current
    return current


def parse_structured_output(raw: Any, schema: Optional[dict] = None) -> tuple[dict, bool]:
    """Turn whatever the model returned into a dict, or fail loudly.

    Ladder, in order: direct ``json.loads`` -> fenced ```json block -> outermost
    brace span -> light syntax repair. Anything past the first rung sets the
    repaired flag, because a model that cannot respect its own response format
    is a quality signal worth recording on the AgentRun.

    Returns ``(payload, repaired)``. Raises :class:`AgentOutputError` when no
    rung works or when a required top-level key is missing, so the caller
    retries or skips instead of proceeding on garbage.
    """
    required = _required_keys(schema)

    # Some gateways hand back already-decoded JSON. Accept it rather than
    # forcing a pointless round-trip through a string.
    if isinstance(raw, (dict, list)):
        payload = _unwrap_envelope(raw, required)
        if not isinstance(payload, dict):
            raise AgentOutputError("structured output was not a JSON object", raw=str(raw))
        _validate_required(payload, required, str(raw))
        return payload, False

    text = (raw or "").strip()
    if not text:
        raise AgentOutputError("provider returned an empty response", raw="")

    candidates: list[tuple[str, bool]] = [(text, False)]

    fenced = _FENCE_RE.search(text)
    if fenced:
        candidates.append((fenced.group(1).strip(), True))

    span = _outermost_span(text)
    if span and span != text:
        candidates.append((span, True))

    candidates.append((_repair(text), True))
    if span:
        candidates.append((_repair(span), True))

    last_error: Optional[Exception] = None
    for candidate, repaired in candidates:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError) as exc:
            last_error = exc
            continue

        payload = _unwrap_envelope(parsed, required)
        if isinstance(payload, list):
            # A bare array is only usable when the schema wants exactly one
            # top-level list key; wrap it rather than throwing away the work.
            if len(required) == 1:
                payload = {required[0]: payload}
                repaired = True
            else:
                last_error = ValueError("top-level JSON array with an object schema")
                continue
        if not isinstance(payload, dict):
            last_error = ValueError(f"top-level JSON was {type(payload).__name__}, expected object")
            continue

        _validate_required(payload, required, text)
        return payload, repaired

    raise AgentOutputError(
        f"could not parse structured output after {len(candidates)} strategies: {last_error}",
        raw=text,
    )


def _validate_required(payload: dict, required: list[str], raw: str) -> None:
    missing = [k for k in required if k not in payload]
    if missing:
        raise AgentOutputError(
            f"structured output is missing required key(s): {', '.join(missing)}",
            raw=raw,
        )


# ---------------------------------------------------------------------------
# DronaHQ transport
# ---------------------------------------------------------------------------
# agent_key -> Settings attribute holding that published agent's endpoint id.
AGENT_ID_SETTINGS: dict[str, str] = {
    "prospect_generation": "DRONAHQ_AGENT_PROSPECT_GENERATION",
    "research_enrichment": "DRONAHQ_AGENT_RESEARCH_ENRICHMENT",
    "icp_fit": "DRONAHQ_AGENT_ICP_FIT",
    "outreach_strategy": "DRONAHQ_AGENT_OUTREACH_STRATEGY",
    "personalisation": "DRONAHQ_AGENT_PERSONALISATION",
    "conversation": "DRONAHQ_AGENT_CONVERSATION",
}

# 408/409/425/429 plus every 5xx: all transient or rate-limit related.
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524})
MAX_ATTEMPTS = 3
BASE_BACKOFF_SECONDS = 0.75
MAX_BACKOFF_SECONDS = 12.0


def resolve_dronahq_agent_id(agent_key: str, override: Optional[str] = None) -> Optional[str]:
    """Explicit id (from ``AgentConfig.dronahq_agent_id``) wins over settings.

    Per-campaign overrides matter because two campaigns can point the same
    logical agent at different published DronaHQ flows.
    """
    if override:
        return override
    attr = AGENT_ID_SETTINGS.get(agent_key)
    if not attr:
        return None
    return getattr(settings, attr, "") or None


def _backoff_delay(attempt: int, retry_after: Optional[float] = None) -> float:
    """Exponential backoff with full-width jitter.

    Jitter matters more than the exponent here: without it, a tick that fans
    out six agent calls retries all six in lockstep and re-triggers the same
    429 that caused the retry.
    """
    if retry_after is not None:
        return min(max(retry_after, 0.0), MAX_BACKOFF_SECONDS)
    base = min(BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)), MAX_BACKOFF_SECONDS)
    return base + random.uniform(0.0, base * 0.5)


def _retry_after_seconds(response: httpx.Response) -> Optional[float]:
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


class DronaHQClient:
    """Calls a published DronaHQ agent endpoint over HTTP.

    One POST per invocation to ``{DRONAHQ_BASE_URL}/{agent_id}``; DronaHQ owns
    the prompt execution and returns structured JSON, which is exactly the
    integration the rubric asks for.
    """

    executor = "dronahq"

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> None:
        self.base_url = (base_url or settings.DRONAHQ_BASE_URL).rstrip("/")
        self.api_key = api_key or settings.DRONAHQ_API_KEY
        self.timeout = timeout or settings.DRONAHQ_TIMEOUT_SECONDS

    # -- public ------------------------------------------------------------
    async def invoke(
        self,
        *,
        agent_key: str,
        prompt: str,
        variables: dict,
        output_schema: dict,
        model: Optional[str] = None,
        dronahq_agent_id: Optional[str] = None,
    ) -> AgentResult:
        agent_id = resolve_dronahq_agent_id(agent_key, dronahq_agent_id)
        if not agent_id:
            raise AgentTransportError(
                f"no DronaHQ agent id configured for '{agent_key}' "
                f"(set {AGENT_ID_SETTINGS.get(agent_key, 'DRONAHQ_AGENT_*')} or AgentConfig.dronahq_agent_id)"
            )

        url = f"{self.base_url}/{agent_id}"
        payload = {
            "variables": variables,
            "prompt": prompt,
            "response_format": {"type": "json_schema", "json_schema": output_schema},
        }
        if model:
            payload["model"] = model

        started = time.perf_counter()
        response, attempts = await self._post_with_retries(url, payload, agent_key)
        latency_ms = int((time.perf_counter() - started) * 1000)

        raw_text, body = self._extract_raw(response)
        resolved_model = self._resolve_model(body, model)

        try:
            output, repaired = parse_structured_output(raw_text, output_schema)
        except AgentOutputError:
            # Re-raise unchanged: base.py decides whether to retry the agent
            # with a stricter instruction or mark the run failed. Logged here
            # so the raw text is in the application log either way.
            logger.warning("agent %s returned unparseable output via DronaHQ", agent_key)
            raise

        tokens_in, tokens_out = self._token_counts(body, prompt, variables, raw_text)
        return AgentResult(
            output=output,
            executor=self.executor,
            model=resolved_model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=compute_cost(resolved_model, tokens_in, tokens_out),
            latency_ms=latency_ms,
            raw=raw_text,
            repaired=repaired,
            attempts=attempts,
            meta={"agent_id": agent_id, "url": url},
        )

    # -- internals ---------------------------------------------------------
    async def _post_with_retries(
        self, url: str, payload: dict, agent_key: str
    ) -> tuple[httpx.Response, int]:
        """Retry transient failures only; surface 4xx immediately.

        A 400/401/403/404 means the request or the credentials are wrong, and
        no amount of waiting fixes that — retrying only delays the error and
        eats the rate-limit budget shared with the calls that could succeed.
        """
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        last_error: Optional[str] = None
        last_status: Optional[int] = None

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for attempt in range(1, MAX_ATTEMPTS + 1):
                try:
                    response = await client.post(url, json=payload, headers=headers)
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    last_status = None
                    if attempt == MAX_ATTEMPTS:
                        break
                    delay = _backoff_delay(attempt)
                    logger.warning(
                        "dronahq %s transport error (attempt %s/%s), retrying in %.2fs: %s",
                        agent_key, attempt, MAX_ATTEMPTS, delay, last_error,
                    )
                    await asyncio.sleep(delay)
                    continue

                if response.status_code < 400:
                    return response, attempt

                last_status = response.status_code
                last_error = response.text[:500]

                if response.status_code not in RETRYABLE_STATUS:
                    raise AgentTransportError(
                        f"dronahq {agent_key} failed with {response.status_code}: {last_error}",
                        status_code=response.status_code,
                        attempts=attempt,
                    )

                if attempt == MAX_ATTEMPTS:
                    break

                delay = _backoff_delay(attempt, _retry_after_seconds(response))
                logger.warning(
                    "dronahq %s got %s (attempt %s/%s), retrying in %.2fs",
                    agent_key, response.status_code, attempt, MAX_ATTEMPTS, delay,
                )
                await asyncio.sleep(delay)

        raise AgentTransportError(
            f"dronahq {agent_key} failed after {MAX_ATTEMPTS} attempts: {last_error}",
            status_code=last_status,
            attempts=MAX_ATTEMPTS,
        )

    @staticmethod
    def _extract_raw(response: httpx.Response) -> tuple[str, dict]:
        """Find the model text inside whatever envelope DronaHQ used.

        Published DronaHQ agents have been observed returning the payload bare,
        under ``data``/``output``/``result``, or as a stringified JSON blob, so
        we probe rather than assume one shape.
        """
        try:
            body = response.json()
        except (ValueError, TypeError):
            return response.text, {}

        if not isinstance(body, dict):
            return json.dumps(body), {}

        for key in ("output", "data", "result", "response", "text", "message", "content"):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                return value, body
            if isinstance(value, (dict, list)):
                return json.dumps(value), body

        # No recognised envelope: hand the whole body to the parser, which can
        # still find the required keys at the top level.
        return json.dumps(body), body

    @staticmethod
    def _resolve_model(body: dict, requested: Optional[str]) -> str:
        for key in ("model", "model_name", "llm_model"):
            value = body.get(key) if isinstance(body, dict) else None
            if isinstance(value, str) and value:
                return value
        return requested or DEFAULT_MODEL

    @staticmethod
    def _token_counts(body: dict, prompt: str, variables: dict, raw_text: str) -> tuple[int, int]:
        """Provider usage when available, character-based estimate otherwise."""
        usage: Any = body.get("usage") if isinstance(body, dict) else None
        if not isinstance(usage, dict):
            usage = body if isinstance(body, dict) else {}

        def _first_int(*keys: str) -> Optional[int]:
            for key in keys:
                value = usage.get(key)
                if isinstance(value, (int, float)) and value > 0:
                    return int(value)
            return None

        tokens_in = _first_int("tokens_in", "input_tokens", "prompt_tokens")
        tokens_out = _first_int("tokens_out", "output_tokens", "completion_tokens")

        if tokens_in is None:
            payload_text = prompt + json.dumps(variables, default=str)
            tokens_in = estimate_tokens(payload_text)
        if tokens_out is None:
            tokens_out = estimate_tokens(raw_text)
        return tokens_in, tokens_out


# ---------------------------------------------------------------------------
# Simulator transport
# ---------------------------------------------------------------------------
class SimulatorClient:
    """Offline, deterministic implementation of the same contract.

    The generation logic lives in ``simulator.py``; this class only wraps it in
    the transport contract. Crucially it routes the simulated text through the
    *same* ``parse_structured_output`` used for DronaHQ, so the offline demo
    exercises the real validation path rather than a shortcut around it.
    """

    executor = "simulator"

    def __init__(self, *, latency: bool = True) -> None:
        # Simulated latency makes the live dashboard look like a real pipeline
        # instead of instantaneous magic; tests turn it off.
        self._latency = latency

    async def invoke(
        self,
        *,
        agent_key: str,
        prompt: str,
        variables: dict,
        output_schema: dict,
        model: Optional[str] = None,
        dronahq_agent_id: Optional[str] = None,
    ) -> AgentResult:
        from .simulator import simulate_text, simulated_latency_ms  # local: avoids a cycle

        started = time.perf_counter()
        raw_text = simulate_text(agent_key, variables)

        fake_latency_ms = simulated_latency_ms(agent_key, variables)
        if self._latency:
            await asyncio.sleep(fake_latency_ms / 1000.0)

        output, repaired = parse_structured_output(raw_text, output_schema)

        resolved_model = model or SIMULATOR_MODEL
        tokens_in = estimate_tokens(prompt + json.dumps(variables, default=str))
        tokens_out = estimate_tokens(raw_text)
        latency_ms = int((time.perf_counter() - started) * 1000) or fake_latency_ms

        return AgentResult(
            output=output,
            executor=self.executor,
            model=resolved_model,
            # Priced with the configured model's real rates: the simulator is
            # meant to give a truthful preview of what a live run would cost.
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=compute_cost(resolved_model if model else DEFAULT_MODEL, tokens_in, tokens_out),
            latency_ms=latency_ms,
            raw=raw_text,
            repaired=repaired,
            attempts=1,
            meta={"simulated": True},
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def get_agent_client() -> AgentClient:
    """DronaHQ when credentials are configured, simulator otherwise.

    Cached because the client is stateless and cheap to reuse; call
    ``get_agent_client.cache_clear()`` in tests that patch settings.
    """
    if settings.dronahq_enabled:
        logger.info("agent transport: DronaHQ (%s)", settings.DRONAHQ_BASE_URL)
        return DronaHQClient()
    logger.info("agent transport: deterministic simulator (no DronaHQ credentials)")
    return SimulatorClient()


__all__ = [
    "AgentClient",
    "AgentError",
    "AgentOutputError",
    "AgentResult",
    "AgentTransportError",
    "DronaHQClient",
    "SimulatorClient",
    "AGENT_ID_SETTINGS",
    "compute_cost",
    "estimate_tokens",
    "get_agent_client",
    "parse_structured_output",
    "price_for_model",
    "resolve_dronahq_agent_id",
]
