"""Outbound channel contract.

Every external touch the platform makes goes through one of these adapters, so
there is exactly one place where "did this actually leave the building?" is
decided. Three rules hold for all of them:

* **Dry run is the default.** ``settings.CHANNELS_DRY_RUN`` short-circuits the
  provider call while the message is still written to the database exactly as
  it would have been sent. The deployed demo runs this way: judges see the
  full funnel, real strangers do not get cold-called by a hackathon build.
* **Adapters never raise.** A provider outage must degrade one message to
  ``failed``, not crash the orchestrator tick and stall every other prospect.
  Failures come back as ``SendResult(ok=False, error=...)``.
* **Validation before transport.** ``validate_recipient`` is a cheap local
  check so an unreachable prospect is blocked without spending an API call or
  a rate-limit slot.
"""
from __future__ import annotations

import logging
import re
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

from ..config import settings

if TYPE_CHECKING:  # pragma: no cover - typing only
    # Imported lazily so the channel layer stays usable (and testable) without
    # the ORM stack loaded.
    from ..models import Campaign, Prospect

logger = logging.getLogger(__name__)

# Deliberately permissive: the job here is to catch "", "n/a" and obvious
# garbage before spending a provider call, not to re-litigate RFC 5322.
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")
# E.164 after stripping the cosmetic characters humans type into CRMs.
PHONE_RE = re.compile(r"^\+?[1-9]\d{7,14}$")
LINKEDIN_RE = re.compile(r"^https?://([a-z]{2,3}\.)?linkedin\.com/", re.IGNORECASE)


@dataclass
class SendResult:
    """Outcome of one send attempt.

    ``dry_run`` is kept separate from ``ok`` on purpose: a dry-run send
    succeeded as far as the pipeline is concerned, but the metrics layer must
    be able to tell a recorded message from a delivered one.
    """

    ok: bool
    external_id: Optional[str] = None
    error: Optional[str] = None
    dry_run: bool = False
    # Non-failure commentary (e.g. "queued for the LinkedIn worker"). Kept out
    # of ``error`` so a successful queue never renders as a red row in the UI.
    detail: Optional[str] = None

    @property
    def status(self) -> str:
        """Maps onto ``Message.status`` so callers need no translation table."""
        if not self.ok:
            return "failed"
        return "queued" if self.dry_run else "sent"


def dry_run_result(prefix: str, detail: Optional[str] = None) -> SendResult:
    """Uniform dry-run receipt.

    The synthetic id is prefixed per channel so an operator reading the
    messages table can tell at a glance which sends were simulated.
    """
    return SendResult(
        ok=True,
        external_id=f"dry_{prefix}_{uuid.uuid4().hex[:16]}",
        dry_run=True,
        detail=detail or "CHANNELS_DRY_RUN is on: recorded in full, not delivered.",
    )


def normalise_phone(raw: Any) -> str:
    """Strip the spaces, dashes and brackets CRMs collect; keep a leading +."""
    text = str(raw or "").strip()
    if not text:
        return ""
    cleaned = re.sub(r"[^\d+]", "", text)
    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]
    return cleaned


class ChannelAdapter(ABC):
    """One outbound channel.

    Subclasses implement ``_deliver``; ``send`` owns the guarantees that must
    hold for every channel (dry-run short circuit, recipient validation, and
    the promise never to raise).
    """

    channel: str = "unknown"
    # Whether this channel can ever perform a real send. LinkedIn cannot,
    # because there is no sanctioned API for it.
    supports_live_send: bool = True

    # -- contract ----------------------------------------------------------
    @abstractmethod
    def validate_recipient(self, recipient: dict) -> Optional[str]:
        """Return an error string when this recipient is unreachable, else None."""

    @abstractmethod
    async def _deliver(
        self,
        *,
        recipient: dict,
        subject: Optional[str],
        body: str,
        campaign: Optional["Campaign"],
        prospect: Optional["Prospect"],
    ) -> SendResult:
        """Perform the real provider call. Only reached outside dry-run mode."""

    # -- public ------------------------------------------------------------
    async def send(
        self,
        *,
        recipient: dict,
        subject: Optional[str] = None,
        body: str,
        campaign: Optional["Campaign"] = None,
        prospect: Optional["Prospect"] = None,
    ) -> SendResult:
        recipient = recipient or {}

        error = self.validate_recipient(recipient)
        if error:
            return SendResult(ok=False, error=error)

        if not (body or "").strip():
            return SendResult(ok=False, error=f"{self.channel}: refusing to send an empty body")

        if self.dry_run_enabled(campaign):
            logger.info("[dry-run] %s send to %s suppressed", self.channel, self.describe_recipient(recipient))
            return dry_run_result(self.channel)

        try:
            return await self._deliver(
                recipient=recipient,
                subject=subject,
                body=body,
                campaign=campaign,
                prospect=prospect,
            )
        except Exception as exc:  # noqa: BLE001 - one bad send must not stop the tick
            logger.exception("%s adapter raised during send", self.channel)
            return SendResult(ok=False, error=f"{self.channel} send failed: {type(exc).__name__}: {exc}")

    # -- helpers for subclasses -------------------------------------------
    def dry_run_enabled(self, campaign: Optional["Campaign"] = None) -> bool:
        """Global switch, with a per-campaign channel override.

        A campaign can force dry-run for one channel (``config.dry_run``) even
        when the platform is live, which is how a new sequence gets smoke
        tested against real data without contacting anyone.
        """
        if settings.CHANNELS_DRY_RUN:
            return True
        if not self.supports_live_send:
            return True
        config = self.channel_config(campaign)
        return bool(config.get("dry_run"))

    def channel_config(self, campaign: Optional["Campaign"]) -> dict:
        """Per-campaign config for this channel, or {} when there is none."""
        for item in getattr(campaign, "channels", None) or []:
            if getattr(item, "type", None) == self.channel:
                return getattr(item, "config", None) or {}
        return {}

    @staticmethod
    def describe_recipient(recipient: dict) -> str:
        """Partly redacted identifier for logs: enough to debug, not a leak."""
        for key in ("email", "phone", "linkedin_url"):
            value = str(recipient.get(key) or "")
            if value:
                if "@" in value:
                    name, _, domain = value.partition("@")
                    return f"{name[:2]}***@{domain}"
                return f"{value[:5]}***{value[-2:]}" if len(value) > 8 else "***"
        return "<no contact details>"


__all__ = [
    "ChannelAdapter",
    "SendResult",
    "EMAIL_RE",
    "LINKEDIN_RE",
    "PHONE_RE",
    "dry_run_result",
    "normalise_phone",
]
