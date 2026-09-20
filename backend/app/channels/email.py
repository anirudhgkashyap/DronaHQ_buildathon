"""Email delivery via the SendGrid v3 HTTP API.

The HTTP API is used rather than SMTP because it returns a provider message id
synchronously (``X-Message-Id``), and that id is what later ties a webhook
delivery/bounce/reply event back to the ``Message`` row that produced it.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

import httpx

from ..config import settings
from .base import EMAIL_RE, ChannelAdapter, SendResult

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..models import Campaign, Prospect

logger = logging.getLogger(__name__)

SENDGRID_URL = "https://api.sendgrid.com/v3/mail/send"
REQUEST_TIMEOUT_SECONDS = 20.0


class EmailAdapter(ChannelAdapter):
    """SendGrid-backed email channel."""

    channel = "email"

    def validate_recipient(self, recipient: dict) -> Optional[str]:
        address = str((recipient or {}).get("email") or "").strip()
        if not address:
            return "email: prospect has no email address on file"
        if not EMAIL_RE.match(address):
            return f"email: '{address}' is not a valid address"
        return None

    def from_address(self, campaign: Optional["Campaign"]) -> tuple[str, Optional[str]]:
        """Sending identity, with the campaign's channel config taking priority.

        Outreach goes out under a rep's identity when the campaign defines one,
        which is both better for deliverability and what the prospect expects
        to see when they hit reply.
        """
        config = self.channel_config(campaign)
        address = str(config.get("from_email") or settings.OUTREACH_FROM_EMAIL or "").strip()
        name = config.get("from_name")
        return address, str(name) if name else None

    async def _deliver(
        self,
        *,
        recipient: dict,
        subject: Optional[str],
        body: str,
        campaign: Optional["Campaign"],
        prospect: Optional["Prospect"],
    ) -> SendResult:
        if not settings.SENDGRID_API_KEY:
            # Explicit failure rather than a silent fallback to dry-run: if the
            # operator turned dry-run off, they expect real delivery and must
            # be told plainly why it did not happen.
            return SendResult(
                ok=False,
                error="email: SENDGRID_API_KEY is not configured (set it, or set CHANNELS_DRY_RUN=true)",
            )

        from_email, from_name = self.from_address(campaign)
        if not from_email:
            return SendResult(ok=False, error="email: no OUTREACH_FROM_EMAIL configured")

        to_address = str(recipient["email"]).strip()
        sender: dict = {"email": from_email}
        if from_name:
            sender["name"] = from_name

        payload = {
            "personalizations": [{"to": [{"email": to_address}]}],
            "from": sender,
            "subject": (subject or "").strip() or "Quick question",
            "content": [{"type": "text/plain", "value": body}],
            # Tracking is left on for opens/clicks: reply-rate metrics on the
            # dashboard are only meaningful with it.
            "tracking_settings": {
                "click_tracking": {"enable": True},
                "open_tracking": {"enable": True},
            },
        }
        if prospect is not None and getattr(prospect, "id", None):
            # Round-trips on every SendGrid event webhook, which is how an
            # inbound event finds its prospect without a lookup table.
            payload["custom_args"] = {
                "prospect_id": str(prospect.id),
                "campaign_id": str(getattr(campaign, "id", "") or ""),
            }

        headers = {
            "Authorization": f"Bearer {settings.SENDGRID_API_KEY}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.post(SENDGRID_URL, json=payload, headers=headers)

        if response.status_code in (200, 201, 202):
            external_id = response.headers.get("X-Message-Id") or response.headers.get("x-message-id")
            return SendResult(ok=True, external_id=external_id)

        return SendResult(
            ok=False,
            error=f"email: sendgrid returned {response.status_code}: {response.text[:400]}",
        )


__all__ = ["EmailAdapter"]
