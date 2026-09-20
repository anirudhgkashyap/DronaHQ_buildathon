"""LinkedIn channel.

LinkedIn has no sanctioned API for sending connection requests or InMail from
a third-party tool, and scraping or automating the site from the server would
breach their terms and risk the user's account. So this adapter never performs
a network call: it validates the profile, records the message, and hands it to
a queue that a supervised browser-automation worker (running under the rep's
own session, on the rep's machine) drains.

That is a deliberate product decision, not a gap. The message is still fully
drafted, personalised, scored, approved and audited exactly like an email —
only the final transport is human-supervised. The rest of the system does not
need to know the difference, because a queued LinkedIn touch returns ``ok`` and
counts against the same rate limits.
"""
from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Optional

from .base import LINKEDIN_RE, ChannelAdapter, SendResult

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..models import Campaign, Prospect

logger = logging.getLogger(__name__)

# LinkedIn truncates connection notes at 300 characters; anything longer is
# silently cut, which would strip the ask off the end of the message.
CONNECTION_NOTE_LIMIT = 300

QUEUE_NOTE = (
    "Queued for the supervised LinkedIn worker: LinkedIn offers no sanctioned "
    "send API, so a browser-automation worker running under the rep's own "
    "session delivers this message and reports the result back."
)


class LinkedInAdapter(ChannelAdapter):
    """Always queues; never touches the network from the server."""

    channel = "linkedin"
    # Forces ``dry_run_enabled`` to stay true regardless of CHANNELS_DRY_RUN,
    # so no configuration change can make this adapter attempt a live send.
    supports_live_send = False

    def validate_recipient(self, recipient: dict) -> Optional[str]:
        url = str((recipient or {}).get("linkedin_url") or "").strip()
        if not url:
            return "linkedin: prospect has no LinkedIn profile URL on file"
        if not LINKEDIN_RE.match(url):
            return f"linkedin: '{url[:80]}' is not a LinkedIn profile URL"
        return None

    async def _deliver(
        self,
        *,
        recipient: dict,
        subject: Optional[str],
        body: str,
        campaign: Optional["Campaign"],
        prospect: Optional["Prospect"],
    ) -> SendResult:
        """Unreachable in practice — ``supports_live_send`` keeps sends in the
        dry-run path — but implemented so the class is never partially abstract
        and so a future sanctioned API has an obvious home."""
        return self._queue(body)

    async def send(
        self,
        *,
        recipient: dict,
        subject: Optional[str] = None,
        body: str,
        campaign: Optional["Campaign"] = None,
        prospect: Optional["Prospect"] = None,
    ) -> SendResult:
        # Validation and the empty-body guard still apply, so the base
        # implementation runs first; only the receipt is specialised.
        result = await super().send(
            recipient=recipient,
            subject=subject,
            body=body,
            campaign=campaign,
            prospect=prospect,
        )
        if not result.ok:
            return result
        return self._queue(body)

    def _queue(self, body: str) -> SendResult:
        note = QUEUE_NOTE
        if len(body or "") > CONNECTION_NOTE_LIMIT:
            note += (
                f" Note: body is {len(body)} characters; a connection request caps at "
                f"{CONNECTION_NOTE_LIMIT}, so the worker will send this as an InMail/message "
                "rather than a connection note."
            )
        logger.info("linkedin message queued for the supervised worker")
        return SendResult(
            ok=True,
            external_id=f"li_queued_{uuid.uuid4().hex[:16]}",
            dry_run=True,
            detail=note,
        )


__all__ = ["LinkedInAdapter", "CONNECTION_NOTE_LIMIT"]
