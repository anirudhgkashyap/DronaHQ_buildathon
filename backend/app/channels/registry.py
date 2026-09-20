"""Channel lookup.

The orchestrator names a channel as a string (it comes out of the strategy
agent's JSON, and out of ``Campaign.policies["channel_priority"]``), so one
lookup table maps that string to an adapter. Adapters are stateless, so a
single shared instance per channel is reused rather than constructed per send.
"""
from __future__ import annotations

from typing import Callable

from .base import ChannelAdapter, SendResult
from .email import EmailAdapter
from .linkedin import LinkedInAdapter
from .sms import SmsAdapter, WhatsAppAdapter
from .voice import VoiceAdapter

# Order matters: it is the default channel priority for a campaign that does
# not set one, running from least to most intrusive.
SUPPORTED_CHANNELS: list[str] = ["email", "linkedin", "sms", "whatsapp", "voice"]

_FACTORIES: dict[str, Callable[[], ChannelAdapter]] = {
    "email": EmailAdapter,
    "linkedin": LinkedInAdapter,
    "sms": SmsAdapter,
    # WhatsApp is the SMS adapter with Twilio's `whatsapp:` prefix applied to
    # both endpoints; it is not a separate integration.
    "whatsapp": WhatsAppAdapter,
    "voice": VoiceAdapter,
}

_INSTANCES: dict[str, ChannelAdapter] = {}


class UnknownChannelError(ValueError):
    """Raised for a channel string no adapter handles.

    A ValueError subclass so a caller that only wants "bad input" semantics
    can catch it without importing this module.
    """


def get_adapter(channel: str) -> ChannelAdapter:
    """Return the shared adapter for ``channel``.

    Raises :class:`UnknownChannelError` rather than returning None, because a
    silent no-op here would look like a delivered message in the funnel.
    """
    key = (channel or "").strip().lower()
    if key not in _FACTORIES:
        raise UnknownChannelError(
            f"unsupported channel '{channel}'; supported: {', '.join(SUPPORTED_CHANNELS)}"
        )
    if key not in _INSTANCES:
        _INSTANCES[key] = _FACTORIES[key]()
    return _INSTANCES[key]


def is_supported(channel: str) -> bool:
    """Cheap membership test for validating agent output before acting on it."""
    return (channel or "").strip().lower() in _FACTORIES


__all__ = [
    "ChannelAdapter",
    "SendResult",
    "SUPPORTED_CHANNELS",
    "UnknownChannelError",
    "get_adapter",
    "is_supported",
]
