"""Contract between the execution worker and target systems.

Failure classification decides what the worker may do next:

- ``PermanentError``  the target rejected the change; nothing was applied. Never retried.
- ``TransientError``  the request provably did not take effect (e.g. connection refused).
                      Retried with backoff.
- ``AmbiguousError``  the request may or may not have taken effect (timeout after send,
                      5xx from a proxy). Never blindly retried: the worker first
                      ``observe``s the target and only re-applies when the effect is absent.

Every apply call carries the action id. Adapters whose API accepts idempotency keys
must forward it; others must make the operation naturally idempotent (PUT-style) and
rely on ``observe`` for reconciliation.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Protocol

# Ordered permission levels; a grant is redundant when the holder already has >= level.
LEVEL_RANK = {"readonly": 1, "reporter": 2, "developer": 3}


class AdapterError(Exception):
    pass


class PermanentError(AdapterError):
    pass


class TransientError(AdapterError):
    pass


class AmbiguousError(AdapterError):
    pass


class Adapter(Protocol):
    name: str

    def level(self, role: str) -> str:
        """Map a policy role onto the permission level the target system can represent."""

    async def preflight(self, action: Dict[str, Any]) -> None:
        """Raise PermanentError if the action cannot succeed (unknown user, missing repo)."""

    async def current_level(self, employee_id: str, system: str) -> Optional[str]:
        """Permission level currently held, or None."""

    async def apply(self, action_id: str, action: Dict[str, Any]) -> Dict[str, Any]:
        """Perform the change. Must be safe to call again with the same action id."""

    async def observe(self, action_id: str, action: Dict[str, Any]) -> bool:
        """Independent read: does the target already reflect this action's intended effect?"""


def rank(level: Optional[str]) -> int:
    return LEVEL_RANK.get(level or "", 0)
