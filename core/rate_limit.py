"""Per-identity rate limiting for requests that start model work.

GCRA (generic cell rate algorithm): one stored timestamp per key, the "theoretical arrival
time". ``limit`` requests per ``period_s`` are allowed, all of them at once if the caller
was idle (burst = limit), then one every ``period_s / limit``. Denied requests do not
consume capacity, so a client that keeps retrying is not locked out longer.

The Redis backend runs the check as one Lua script using the Redis clock, so every API
process shares the same budget and process clocks do not matter. When Redis errors the
check fails open: the limiter protects capacity and cost, while sessions, idempotency
and approvals are protected elsewhere, and refusing every request would be a worse outage.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RateRule:
    limit: int
    period_s: float

    @property
    def interval(self) -> float:
        return self.period_s / self.limit

    @classmethod
    def parse(cls, text: Optional[str], default: Optional["RateRule"]) -> Optional["RateRule"]:
        """"30/60" = 30 requests per 60 s; "off" or "0" disables; anything unparsable keeps the default."""
        value = (text or "").strip().lower()
        if not value:
            return default
        if value in {"0", "off", "false", "no", "none"}:
            return None
        try:
            limit, period = value.split("/", 1)
            rule = cls(int(limit), float(period))
        except ValueError:
            logger.warning("ignoring malformed rate limit %r; keeping %s", text, default)
            return default
        if rule.limit < 1 or not math.isfinite(rule.period_s) or rule.period_s <= 0:
            logger.warning("ignoring malformed rate limit %r; keeping %s", text, default)
            return default
        return rule


@dataclass(frozen=True)
class Decision:
    allowed: bool
    retry_after_s: float = 0.0


def _gcra(tat: float, now: float, rule: RateRule) -> tuple[Decision, Optional[float]]:
    """Returns the decision and the new theoretical arrival time to store (None when denied)."""
    tat = max(tat, now)
    allow_at = tat + rule.interval - rule.period_s
    if now < allow_at:
        return Decision(False, allow_at - now), None
    return Decision(True), tat + rule.interval


_LUA = """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local interval = tonumber(ARGV[1])
local period = tonumber(ARGV[2])
local tat = tonumber(redis.call('GET', KEYS[1]) or now)
if tat < now then tat = now end
local allow_at = tat + interval - period
if now < allow_at then
  return {0, tostring(allow_at - now)}
end
local new_tat = tat + interval
redis.call('SET', KEYS[1], tostring(new_tat), 'PX', math.ceil((new_tat - now) * 1000) + 1)
return {1, '0'}
"""


class MemoryRateLimiter:
    """Single-process backend for tests and Redis-less local runs."""

    def __init__(self, rules: Dict[str, RateRule], clock: Callable[[], float] = time.monotonic):
        self.rules, self.clock = rules, clock
        self._tat: Dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def check(self, bucket: str, scope_key: str) -> Decision:
        rule = self.rules.get(bucket)
        if rule is None:
            return Decision(True)
        key = f"{bucket}:{scope_key}"
        async with self._lock:
            now = self.clock()
            decision, tat = _gcra(self._tat.get(key, now), now, rule)
            if tat is not None:
                self._tat[key] = tat
            if len(self._tat) > 10_000:  # forget idle keys; an idle key is equivalent to a missing one
                self._tat = {k: v for k, v in self._tat.items() if v > now}
        return decision


class RedisRateLimiter:
    def __init__(self, redis, rules: Dict[str, RateRule], prefix: str = "ratelimit"):
        self.redis, self.rules, self.prefix = redis, rules, prefix
        self._script = redis.register_script(_LUA)

    async def check(self, bucket: str, scope_key: str) -> Decision:
        rule = self.rules.get(bucket)
        if rule is None:
            return Decision(True)
        try:
            allowed, retry = await self._script(keys=[f"{self.prefix}:{bucket}:{scope_key}"], args=[rule.interval, rule.period_s])
        except Exception as exc:  # fail open, see module docstring
            logger.warning("rate limit check failed open: %s", type(exc).__name__)
            return Decision(True)
        return Decision(bool(int(allowed)), float(retry))


# Requests that start model work share one budget per identity: /chat, /chat/stream,
# shared-task messages that ask the agent, and IM messages.
AGENT_TURN = "agent_turn"
DEFAULT_RULES = {AGENT_TURN: RateRule(30, 60.0)}


def rules_from_env(getenv) -> Dict[str, RateRule]:
    rule = RateRule.parse(getenv("IT_COPILOT_RATE_LIMIT_AGENT_TURN"), DEFAULT_RULES[AGENT_TURN])
    return {AGENT_TURN: rule} if rule else {}
