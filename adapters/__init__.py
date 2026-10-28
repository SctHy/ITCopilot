"""Target-system adapters and routing."""
from __future__ import annotations

from typing import Any, Dict, Iterable, Optional

from adapters.base import Adapter


class AdapterRegistry:
    """Routes an action to the adapter owning its system; everything else stays local."""

    def __init__(self, local: Adapter, external: Optional[Dict[str, Adapter]] = None):
        self.local = local
        self.external = dict(external or {})   # policy system -> adapter

    def for_action(self, action: Dict[str, Any]) -> Adapter:
        if action["tool_name"] in {"request_access", "revoke_access"}:
            return self.external.get(action["arguments"]["system"], self.local)
        return self.local

    def by_name(self, name: str) -> Adapter:
        for adapter in (self.local, *self.external.values()):
            if adapter.name == name:
                return adapter
        raise KeyError(name)

    def all(self) -> Iterable[Adapter]:
        return dict.fromkeys((self.local, *self.external.values())).keys()
