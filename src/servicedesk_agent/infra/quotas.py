"""Per-tenant quotas (ADR 0035), stored in ``tenants.quotas``:

- ``agent_turn``: ``"N/seconds"`` requests that start model work, for the whole tenant, checked after
  the per-identity limit; ``"off"`` disables it for the tenant. Default ``DEFAULT_AGENT_TURN``.
- ``daily_cost_usd``: model cost per UTC day before write routes degrade to read-only (ADR 0030).
"""
from __future__ import annotations

import math
from typing import Any, Dict, Mapping, Optional

from servicedesk_agent.infra.rate_limit import RateRule

KEYS = ("agent_turn", "daily_cost_usd")
DEFAULT_AGENT_TURN = "120/60"


class QuotaError(ValueError):
    pass


def validate(quotas: Mapping[str, Any]) -> Dict[str, Any]:
    """Refuse unknown keys and malformed values when a quota is written, not when it is first used."""
    if not isinstance(quotas, Mapping):
        raise QuotaError("quotas must be a JSON object")
    unknown = set(quotas) - set(KEYS)
    if unknown:
        raise QuotaError(f"unknown quota keys {sorted(unknown)}; known: {list(KEYS)}")
    clean = dict(quotas)
    if "agent_turn" in clean:
        value = str(clean["agent_turn"]).strip().lower()
        if value not in {"off", "0"} and RateRule.parse(value, None) is None:
            raise QuotaError(f"agent_turn must be 'N/seconds' or 'off', got {clean['agent_turn']!r}")
        clean["agent_turn"] = value
    if "daily_cost_usd" in clean:
        cost = clean["daily_cost_usd"]
        if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost < 0:
            raise QuotaError(f"daily_cost_usd must be a non-negative number, got {cost!r}")
    return clean


def turn_rule(row: Optional[Mapping[str, Any]]) -> Optional[RateRule]:
    """The tenant-wide turn rate for a registry row (None: no tenant limit)."""
    quotas = (row or {}).get("quotas") or {}
    return RateRule.parse(quotas.get("agent_turn", DEFAULT_AGENT_TURN), None)
