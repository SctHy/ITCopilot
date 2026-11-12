"""Target-system adapters and routing."""
from __future__ import annotations

from typing import Any, Dict, Iterable, Optional, Set

from core.tenant_context import current_tenant

from workflow.adapters.base import Adapter


class AdapterRegistry:
    """Routes an action to the adapter owning its system; everything else stays local.

    Per tenant (ADR 0025): a tenant with adapters of its own (``tenants``) uses only those - its own
    target systems and credentials. ``external`` (the deployment's env-configured adapters) serves
    the tenants in ``shared_with``; None means everyone (one organisation per deployment). The
    tenant is the one in scope: the worker enters each action's tenant before routing it.
    """

    def __init__(self, local: Adapter, external: Optional[Dict[str, Adapter]] = None, *,
                 tenants: Optional[Dict[str, Dict[str, Adapter]]] = None, shared_with: Optional[Set[str]] = None):
        self.local = local
        self.external = dict(external or {})   # policy system -> adapter
        self.tenants = {t: dict(a) for t, a in (tenants or {}).items()}
        self.shared_with = shared_with

    def _external(self) -> Dict[str, Adapter]:
        tenant = current_tenant()
        if tenant in self.tenants:
            return self.tenants[tenant]
        if self.shared_with is None or tenant in self.shared_with:
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
        tenant_adapters = [a for adapters in self.tenants.values() for a in adapters.values()]
        return dict.fromkeys((self.local, *self.external.values(), *tenant_adapters)).keys()
