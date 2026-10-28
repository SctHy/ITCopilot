"""Single governance path for every tool an agent calls.

Before this layer only knowledge search went through MCPToolManager; agent tools were
invoked directly. Now each call gets:

- a per-tool circuit breaker (shared with MCPToolManager's three-state implementation),
- a timeout (sync handlers run in a worker thread so the timeout can fire; the thread
  itself cannot be killed, which is why side-effecting tools are never retried),
- retries for read-only tools only, with jittered backoff,
- an idempotency key for side-effecting tools, recorded in the trace.

Validation errors (``ValueError``) are caller mistakes: they are returned to the model
and do not count against the breaker.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import random
from typing import Any, Dict

from monitor import metrics
from tooling.tool_manager import CircuitBreaker


class ToolUnavailable(RuntimeError):
    """Breaker open: the tool failed repeatedly and is cooling down."""


class ToolTimeout(RuntimeError):
    pass


class ToolExecutor:
    def __init__(self, *, timeout_s: float = 10.0, read_retries: int = 1, failure_threshold: int = 5, recovery_s: float = 30.0,
                 backoff_s: float = 0.2):
        self.timeout_s = timeout_s
        self.read_retries = read_retries
        self.failure_threshold = failure_threshold
        self.recovery_s = recovery_s
        self.backoff_s = backoff_s
        self._breakers: Dict[str, CircuitBreaker] = {}

    def breaker(self, name: str) -> CircuitBreaker:
        if name not in self._breakers:
            self._breakers[name] = CircuitBreaker(self.failure_threshold, self.recovery_s)
        return self._breakers[name]

    def snapshot(self) -> Dict[str, Any]:
        return {name: {"state": b.state.value, "consecutive_failures": b.fail_count} for name, b in self._breakers.items()}

    @staticmethod
    def idempotency_key(request_id: str, name: str, args: Dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps([request_id, name, args], sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    async def run(self, spec, req, args: Dict[str, Any]) -> Any:
        breaker = self.breaker(spec.name)
        if not breaker.allow():
            self._state(spec.name, breaker)
            raise ToolUnavailable(f"工具 {spec.name} 连续失败，已熔断，请稍后重试")
        timeout = getattr(spec, "timeout_s", None) or self.timeout_s
        attempts = 1 + (self.read_retries if spec.retry_safe else 0)
        try:
            for attempt in range(1, attempts + 1):
                generation = breaker.generation
                try:
                    result = await asyncio.wait_for(self._call(spec, req, args), timeout=timeout)
                except ValueError:
                    breaker.record_success(generation)  # the tool works; the input was wrong
                    raise
                except TimeoutError:
                    breaker.record_failure(generation)
                    if attempt == attempts:
                        raise ToolTimeout(f"工具 {spec.name} 执行超过 {timeout:g}s" + ("" if spec.retry_safe else "，结果未知，未自动重试")) from None
                except Exception:
                    breaker.record_failure(generation)
                    if attempt == attempts:
                        raise
                else:
                    breaker.record_success(generation)
                    return result
                await asyncio.sleep(self.backoff_s * attempt * (0.5 + random.random()))
        finally:
            self._state(spec.name, breaker)

    @staticmethod
    async def _call(spec, req, args):
        if inspect.iscoroutinefunction(spec.handler):
            return await spec.handler(req, args)
        result = await asyncio.to_thread(spec.handler, req, args)
        return await result if inspect.isawaitable(result) else result

    @staticmethod
    def _state(name: str, breaker: CircuitBreaker) -> None:
        metrics.TOOL_CIRCUIT_STATE.labels(tool=name).set(metrics.CIRCUIT_STATE_VALUES[breaker.state.value])
