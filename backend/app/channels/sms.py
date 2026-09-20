"""SMS and WhatsApp delivery via the Twilio Messages API.

One adapter serves both channels because Twilio treats WhatsApp as the same
Messages resource with a ``whatsapp:`` prefix on both endpoints. Keeping them
in one class means a fix to segmentation, validation or error handling lands
on both channels at once.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

import httpx

from ..config import settings
from .base import PHONE_RE, ChannelAdapter, SendResult, normalise_phone

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..models import Campaign, Prospect

logger = logging.getLogger(__name__)

TWILIO_API_ROOT = "https://api.twilio.com/2010-04-01"
REQUEST_TIMEOUT_SECONDS = 20.0
# Beyond this a message is split into billed segments and reads as spam on a
# cold first touch, so it is worth flagging rather than silently sending.
SINGLE_SEGMENT_CHARS = 320


class SmsAdapter(ChannelAdapter):
    """Twilio SMS. Also the base for WhatsApp via the ``whatsapp`` flag."""

    channel = "sms"
    whatsapp = False

    def __init__(self, *, whatsapp: bool = False) -> None:
        if whatsapp:
            self.channel = "whatsapp"
            self.whatsapp = True

    # -- validation --------------------------------------------------------
    def validate_recipient(self, recipient: dict) -> Optional[str]:
        phone = normalise_phone((recipient or {}).get("phone"))
        if not phone:
            return f"{self.channel}: prospect has no phone number on file"
        if not PHONE_RE.match(phone):
            return f"{self.channel}: '{phone}' is not a usable E.164 number"
        return None

    # -- addressing --------------------------------------------------------
    def _prefix(self, number: str) -> str:
        """WhatsApp addresses are the same numbers behind a scheme prefix."""
        if not number:
            return number
        if self.whatsapp and not number.startswith("whatsapp:"):
            return f"whatsapp:{number}"
        return number

    def from_number(self, campaign: Optional["Campaign"]) -> str:
        config = self.channel_config(campaign)
        raw = str(config.get("from_number") or settings.TWILIO_FROM_NUMBER or "").strip()
        return self._prefix(normalise_phone(raw) if not raw.startswith("whatsapp:") else raw)

    # -- delivery ----------------------------------------------------------
    async def _deliver(
        self,
        *,
        recipient: dict,
        subject: Optional[str],
        body: str,
        campaign: Optional["Campaign"],
        prospect: Optional["Prospect"],
    ) -> SendResult:
        if not (settings.TWILIO_ACCOUNT_SID and settings.TWILIO_AUTH_TOKEN):
            return SendResult(
                ok=False,
                error=(
                    f"{self.channel}: TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN are not configured "
                    "(set them, or set CHANNELS_DRY_RUN=true)"
                ),
            )

        from_number = self.from_number(campaign)
        if not from_number:
            return SendResult(ok=False, error=f"{self.channel}: no TWILIO_FROM_NUMBER configured")

        to_number = self._prefix(normalise_phone(recipient["phone"]))
        text = body.strip()
        if len(text) > SINGLE_SEGMENT_CHARS:
            logger.warning(
                "%s body is %s chars and will be split into multiple billed segments",
                self.channel, len(text),
            )

        url = f"{TWILIO_API_ROOT}/Accounts/{settings.TWILIO_ACCOUNT_SID}/Messages.json"
        form = {"To": to_number, "From": from_number, "Body": text}

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.post(
                url,
                data=form,
                auth=(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN),
            )

        if response.status_code in (200, 201):
            try:
                sid = response.json().get("sid")
            except (ValueError, TypeError):
                sid = None
            return SendResult(ok=True, external_id=sid)

        return SendResult(ok=False, error=_twilio_error(self.channel, response))


class WhatsAppAdapter(SmsAdapter):
    """WhatsApp over Twilio: the SMS adapter with the prefix switched on."""

    def __init__(self) -> None:
        super().__init__(whatsapp=True)


def _twilio_error(channel: str, response: httpx.Response) -> str:
    """Twilio's JSON error carries a far more useful message than the status."""
    try:
        body = response.json()
        message = body.get("message") or body.get("detail") or response.text
        code = body.get("code")
        suffix = f" (twilio code {code})" if code else ""
        return f"{channel}: twilio {response.status_code}: {str(message)[:300]}{suffix}"
    except (ValueError, TypeError):
        return f"{channel}: twilio {response.status_code}: {response.text[:300]}"


__all__ = ["SmsAdapter", "WhatsAppAdapter"]
