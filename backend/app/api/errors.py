"""The error envelope and the handlers that guarantee it.

The frontend parses exactly one failure shape::

    {"error": {"code": "invalid_transition", "message": "Only a live campaign can be paused."}}

``js/api.js`` reads ``error.message`` and drops it straight into a toast, so
every message in this codebase is written for a sales manager: what happened,
and what they can do about it. Codes are stable and machine-readable; messages
are prose. Neither ever carries a stack trace, a SQL fragment or an internal
identifier the user cannot act on.

Three handlers cover everything that can go wrong:

* :class:`ApiError` - the deliberate failures routers raise.
* ``RequestValidationError`` / ``HTTPException`` - FastAPI's own rejections,
  rewritten into the envelope so the UI never meets FastAPI's ``detail`` shape.
* ``Exception`` - the catch-all. It logs the real traceback server-side and
  returns a generic sentence, because an unexpected failure is the one case
  where leaking internals is most likely and least useful.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class ApiError(Exception):
    """A failure the API meant to produce.

    ``status`` is the HTTP code, ``code`` is the stable slug the UI may branch
    on, and ``message`` is the sentence a manager reads. Raising this is always
    preferred over ``HTTPException`` so the envelope is produced in one place.
    """

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        context: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.context = context or {}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ApiError({self.status}, {self.code!r}, {self.message!r})"


def envelope(code: str, message: str) -> dict:
    """The one and only error body shape."""
    return {"error": {"code": code, "message": message}}


# ---------------------------------------------------------------------------
# Constructors for the failures that recur across routers.
# Centralised so the same situation always produces the same wording, which is
# what makes the UI's toasts feel like one product rather than nine routers.
# ---------------------------------------------------------------------------
def not_found(what: str, identifier: str | None = None) -> ApiError:
    detail = f" ({identifier})" if identifier else ""
    return ApiError(
        404,
        "not_found",
        f"That {what} no longer exists{detail}. It may have been deleted — refresh and try again.",
    )


def invalid_transition(message: str) -> ApiError:
    """409: the action is understood but the entity is in the wrong state.

    Usually means a colleague changed it first, so the message should point at
    the server's state rather than blaming the click.
    """
    return ApiError(409, "invalid_transition", message)


def invalid_state(message: str) -> ApiError:
    """409: the edit is legal in principle but unsafe right now."""
    return ApiError(409, "invalid_state", message)


def invalid_request(message: str) -> ApiError:
    """422: the request itself does not make sense."""
    return ApiError(422, "invalid_request", message)


def blocked(code: str, message: str) -> ApiError:
    """409: a guardrail refused. ``code`` is the guardrail's own code so the UI
    can group blocks by cause (kill switch, suppression, rate limit...)."""
    return ApiError(409, code, message)


# Codes for the HTTP statuses Starlette raises on its own behalf.
_STATUS_CODES = {
    400: "bad_request",
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    422: "invalid_request",
    429: "rate_limited",
    500: "internal_error",
    503: "unavailable",
}

_GENERIC = {
    401: "Your session has expired. Sign in again to continue.",
    403: "You do not have permission to do that.",
    404: "We could not find that. It may have been deleted — refresh and try again.",
    405: "That action is not available here.",
    500: "Something went wrong on our side. The team has been notified — try again in a moment.",
}


def _readable_validation_message(exc: RequestValidationError) -> str:
    """Turn pydantic's error list into one sentence a manager can act on.

    Only the first problem is reported: a toast is one line, and a manager who
    fixes the first field will see the next one immediately.
    """
    errors = exc.errors() or []
    if not errors:
        return "Some of the details you sent are not valid. Check the form and try again."

    first = errors[0]
    location = [str(part) for part in first.get("loc", []) if part not in ("body", "query", "path")]
    field = " › ".join(location) if location else "One of the fields"
    reason = str(first.get("msg", "is not valid")).removeprefix("Value error, ")
    reason = reason[0].lower() + reason[1:] if reason else "is not valid"
    return f"{field} {reason}."


def install_error_handlers(app: FastAPI) -> None:
    """Attach every handler. ``main.py`` calls this once, after mounting."""

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        # Deliberate failures are expected traffic, not incidents: log at info
        # so a noisy approval queue does not look like an outage.
        logger.info("api error %s %s: %s", exc.status, exc.code, exc.message)
        return JSONResponse(status_code=exc.status, content=envelope(exc.code, exc.message))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=envelope("invalid_request", _readable_validation_message(exc)),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _STATUS_CODES.get(exc.status_code, "error")
        # Starlette's own detail ("Not Found") is developer-speak; prefer our
        # wording unless a caller deliberately supplied a readable string.
        detail = exc.detail if isinstance(exc.detail, str) else ""
        message = detail if detail and " " in detail else _GENERIC.get(
            exc.status_code, "Something went wrong. Try again."
        )
        return JSONResponse(status_code=exc.status_code, content=envelope(code, message))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # The traceback belongs in the logs, never in the response body.
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content=envelope("internal_error", _GENERIC[500]))


__all__ = [
    "ApiError",
    "envelope",
    "install_error_handlers",
    "not_found",
    "invalid_transition",
    "invalid_state",
    "invalid_request",
    "blocked",
]
