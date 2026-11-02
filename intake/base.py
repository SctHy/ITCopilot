"""Contract between the channel service and an IM platform."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Protocol


class ChannelError(Exception):
    """A send failed. ``permanent``: the conversation can never receive it (message recalled,
    bot removed from the chat); otherwise the send is retried with backoff."""

    def __init__(self, message: str, *, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent


@dataclass
class InboundMessage:
    id: str                              # "<channel>:<platform message id>"
    channel: str
    conv_id: str
    sender: Dict[str, Any]               # platform identifiers, resolved to a Principal by the channel's directory
    text: str
    address: Dict[str, Any]              # reply target
    addressed: bool                      # mentions the bot or is a direct message
    thread_root: Optional[str] = None    # inbox id of the message that opened the thread, if any
    meta: Dict[str, Any] = field(default_factory=dict)


class Sender(Protocol):
    name: str

    async def send(self, address: Dict[str, Any], text: str, idempotency_key: str) -> None:
        """Deliver text; raise ChannelError. Must be safe to repeat with the same key."""

    async def acknowledge(self, address: Dict[str, Any]) -> None:
        """Best-effort receipt signal (e.g. a reaction) that costs nobody a notification."""
