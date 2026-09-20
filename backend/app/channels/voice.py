"""Outbound voice calls via the Twilio Calls API.

The call is placed with inline TwiML rather than a hosted webhook URL so the
platform needs no publicly reachable callback host to run a voice touch — one
less piece of infrastructure between a demo and a working call.

Voice is the most intrusive channel the platform has, which is why the
strategy agent only selects it late in a sequence and why the body is spoken
verbatim: a script that was drafted, evidence-checked and (optionally)
human-approved is exactly what gets read out, with nothing improvised.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional
from xml.sax.saxutils import escape as xml_escape

import httpx

from ..config import settings
from .base import PHONE_RE, ChannelAdapter, SendResult, normalise_phone

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..models import Campaign, Prospect

logger = logging.getLogger(__name__)

TWILIO_API_ROOT = "https://api.twilio.com/2010-04-01"
REQUEST_TIMEOUT_SECONDS = 25.0
DEFAULT_VOICE = "Polly.Joanna"
# Roughly 150 spoken words per minute; past this a cold call is hung up on.
MAX_SPOKEN_CHARS = 1200


def build_twiml(body: str, *, voice: str = DEFAULT_VOICE, language: str = "en-US") -> str:
    """Wrap the drafted script in TwiML.

    Escaping is not optional: an apostrophe or ampersand in generated copy
    would otherwise produce invalid XML and Twilio would reject the call.
    """
    spoken = (body or "").strip()[:MAX_SPOKEN_CHARS]
    return (
        "<Response>"
        f'<Say voice="{xml_escape(voice, {chr(34): "&quot;"})}" language="{xml_escape(language)}">'
        f"{xml_escape(spoken)}"
        "</Say>"
        # A short pause keeps the line open long enough for a human to respond
        # instead of the call cutting off the instant the script ends.
        '<Pause length="2"/>'
        "</Response>"
    )


class VoiceAdapter(ChannelAdapter):
    """Twilio-backed outbound calling."""

    channel = "voice"

    def validate_recipient(self, recipient: dict) -> Optional[str]:
        phone = normalise_phone((recipient or {}).get("phone"))
        if not phone:
            return "voice: prospect has no phone number on file"
        if not PHONE_RE.match(phone):
            return f"voice: '{phone}' is not a usable E.164 number"
        return None

    def from_number(self, campaign: Optional["Campaign"]) -> str:
        config = self.channel_config(campaign)
        return normalise_phone(config.get("from_number") or settings.TWILIO_FROM_NUMBER)

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
                    "voice: TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN are not configured "
                    "(set them, or set CHANNELS_DRY_RUN=true)"
                ),
            )

        from_number = self.from_number(campaign)
        if not from_number:
            return SendResult(ok=False, error="voice: no TWILIO_FROM_NUMBER configured")

        config = self.channel_config(campaign)
        twiml = build_twiml(
            body,
            voice=str(config.get("voice") or DEFAULT_VOICE),
            language=str(config.get("language") or "en-US"),
        )

        url = f"{TWILIO_API_ROOT}/Accounts/{settings.TWILIO_ACCOUNT_SID}/Calls.json"
        form = {
            "To": normalise_phone(recipient["phone"]),
            "From": from_number,
            "Twiml": twiml,
            # Voicemail is worse than no call for a cold touch: a machine
            # answer ends the attempt instead of talking to an answerphone.
            "MachineDetection": "Enable",
            "Timeout": "25",
        }

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

        try:
            body_json = response.json()
            message = body_json.get("message") or response.text
        except (ValueError, TypeError):
            message = response.text
        return SendResult(ok=False, error=f"voice: twilio {response.status_code}: {str(message)[:300]}")


__all__ = ["VoiceAdapter", "build_twiml", "DEFAULT_VOICE"]
