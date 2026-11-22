"""Target-system adapters and routing."""
from __future__ import annotations

from typing import Any, Dict, Iterable, Optional, Set

from it_copilot.infra.tenant_context import current_tenant

from it_copilot.workflow.adapters.base import Adapter


class AdapterRegistry:
    """Routes an action to the adapter owning its system; everything else stays local.

    ``external`` (the deployment's env-configured adapters) acts only for the tenants in
    ``shared_with``; None means everyone (one organisation per deployment). Other tenants get the
    local sandbox, so one tenant's grants never land in another organisation's systems. The tenant
    is the one in scope: the worker enters each action's tenant before routing it (ADR 0025).
    """

    def __init__(self, local: Adapter, external: Optional[Dict[str, Adapter]] = None, *, shared_with: Optional[Set[str]] = None):
        self.local = local
        self.external = dict(external or {})   # policy system -> adapter
        self.shared_with = shared_with

    def _external(self) -> Dict[str, Adapter]:
        if self.shared_with is None or current_tenant() in self.shared_with:
            return self.external
        return {}

    def for_action(self, action: Dict[str, Any]) -> Adapter:
        if action["tool_name"] in {"request_access", "revoke_access"}:
            return self._external().get(action["arguments"]["system"], self.local)
        return self.local

    def by_name(self, name: str) -> Adapter:
        for adapter in (self.local, *self._external().values()):
            if adapter.name == name:
                return adapter
        raise KeyError(name)

    def all(self) -> Iterable[Adapter]:
        return dict.fromkeys((self.local, *self.external.values())).keys()
