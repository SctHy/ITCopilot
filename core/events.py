"""Execution event log for live SSE.

Producers (API turn, orchestrator, tools, workflow worker) append events while work is
in progress; SSE subscribers read them as they arrive. Events are persisted per stream
(Redis Streams in deployments) so a client that reconnects with ``Last-Event-ID``
resumes after the last event it saw instead of re-running the request.

Payloads are redacted. Status records are size-capped; the completed event carries
the full response required by the chat protocol and must not be replaced by a summary.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import OrderedDict
from enum import StrEnum
from typing import Any, Dict, List, Optional, Tuple

from core.redaction import redact
from core.states import Machine, IllegalTransition


class StreamState(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    ERROR = "error"


STREAM = Machine(StreamState, "running -> completed | error")


class StreamConflict(ValueError):
    pass


def request_fingerprint(message: str, conv_id: str) -> str:
    return hashlib.sha256(json.dumps([message, conv_id], ensure_ascii=False).encode()).hexdigest()


def _binding(raw):
    if not raw:
        return None
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {"owner": raw}
    except (ValueError, TypeError):
        return {"owner": raw}  # streams created by an older deployment


def _check_binding(binding, owner, fingerprint):
    if binding and binding["owner"] == owner and fingerprint is not None and binding.get("fingerprint") != fingerprint:
        raise StreamConflict("同一 client_request_id 不能用于不同消息或会话；旧版流请使用新请求 ID")


def _next_state(previous, event):
    if previous != StreamState.RUNNING:
        raise IllegalTransition(f"stream {previous} cannot publish {event}")
    return STREAM.check(previous, StreamState(event) if event in TERMINAL_EVENTS else StreamState.RUNNING)

TERMINAL_EVENTS = frozenset({"completed", "error"})
STREAM_TTL_S = 3600
MAX_EVENTS = 2000
MAX_PAYLOAD_CHARS = 16000

Event = Tuple[str, str, Dict[str, Any]]  # (id, type, data)


def _encode(data: Dict[str, Any], event: str = "") -> str:
    text = json.dumps(redact(data), ensure_ascii=False, default=str)
    if event != "completed" and len(text) > MAX_PAYLOAD_CHARS:
        text = json.dumps({"truncated": True, "keys": sorted(data)[:50]}, ensure_ascii=False)
    return text


class InMemoryEventBus:
    """Single-process fallback (tests, Redis-less dev). Bounded number of streams."""

    def __init__(self, max_streams: int = 1000):
        self._streams: "OrderedDict[str, List[Tuple[str, str, str]]]" = OrderedDict()
        self._owners: Dict[str, str] = {}
        self._bindings = {}
        self._states = {}
        self._cond = asyncio.Condition()
        self._max_streams = max_streams
        self._seq = 0

    async def claim(self, stream: str, owner: str, fingerprint: Optional[str] = None) -> bool:
        """Register the stream owner; False if the stream already exists (duplicate request)."""
        async with self._cond:
            if stream in self._owners:
                _check_binding(self._bindings.get(stream), owner, fingerprint)
                return False
            self._owners[stream] = owner
            self._bindings[stream] = {"owner": owner, "fingerprint": fingerprint}
            self._states[stream] = StreamState.RUNNING
            self._streams[stream] = []
            while len(self._streams) > self._max_streams:
                old, _ = self._streams.popitem(last=False)
                self._owners.pop(old, None)
                self._bindings.pop(old, None)
                self._states.pop(old, None)
            return True

    async def owner(self, stream: str) -> Optional[str]:
        return self._owners.get(stream)

    async def publish(self, stream: str, event: str, data: Dict[str, Any]) -> str:
        async with self._cond:
            self._states[stream] = _next_state(self._states.get(stream, StreamState.RUNNING), event)
            self._seq += 1
            event_id = f"{int(time.time() * 1000)}-{self._seq}"
            events = self._streams.setdefault(stream, [])
            events.append((event_id, event, _encode(data, event)))
            del events[:-MAX_EVENTS]
            self._cond.notify_all()
            return event_id

    async def status(self, stream: str):
        return self._states.get(stream)

    async def read(self, stream: str, after: Optional[str], block_s: float) -> List[Event]:
        def pending():
            events = self._streams.get(stream, [])
            if after:
                index = next((i for i, e in enumerate(events) if e[0] == after), None)
                events = events[index + 1:] if index is not None else [e for e in events if _id_key(e[0]) > _id_key(after)]
            return [(i, t, json.loads(d)) for i, t, d in events]

        async with self._cond:
            found = pending()
            if found or block_s <= 0:
                return found
            try:
                await asyncio.wait_for(self._cond.wait_for(lambda: bool(pending())), timeout=block_s)
            except TimeoutError:
                return []
            return pending()


def _id_key(event_id: str) -> Tuple[int, int]:
    ms, _, seq = event_id.partition("-")
    try:
        return int(ms), int(seq or 0)
    except ValueError:
        return 0, 0


class RedisEventBus:
    """Redis Streams: XADD per event, XREAD BLOCK for subscribers; ids are Last-Event-IDs."""

    def __init__(self, redis_client, prefix: str = "events:"):
        self._redis = redis_client
        self._prefix = prefix

    async def claim(self, stream: str, owner: str, fingerprint: Optional[str] = None) -> bool:
        key = f"{self._prefix}owner:{stream}"
        # Owner and content are one SET NX value: never expose a half-bound request.
        value = json.dumps({"owner": owner, "fingerprint": fingerprint})
        while True:
            if await self._redis.set(key, value, nx=True, ex=STREAM_TTL_S):
                return True
            binding = _binding(await self._redis.get(key))
            if binding is not None:
                _check_binding(binding, owner, fingerprint)
                return False

    async def owner(self, stream: str) -> Optional[str]:
        binding = _binding(await self._redis.get(f"{self._prefix}owner:{stream}"))
        return binding["owner"] if binding else None

    async def status(self, stream: str):
        value = await self._redis.get(f"{self._prefix}state:{stream}")
        return value or (StreamState.RUNNING if await self.owner(stream) else None)

    async def publish(self, stream: str, event: str, data: Dict[str, Any]) -> str:
        from redis.exceptions import WatchError
        key = self._prefix + stream
        state_key = f"{self._prefix}state:{stream}"
        while True:
            async with self._redis.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(state_key)
                    previous = await pipe.get(state_key) or StreamState.RUNNING
                    target = _next_state(previous, event)
                    pipe.multi()
                    pipe.xadd(key, {"type": event, "data": _encode(data, event)}, maxlen=MAX_EVENTS, approximate=True)
                    pipe.set(state_key, str(target), ex=STREAM_TTL_S)
                    pipe.expire(key, STREAM_TTL_S)
                    pipe.expire(f"{self._prefix}owner:{stream}", STREAM_TTL_S)
                    event_id, *_ = await pipe.execute()
                    return event_id if isinstance(event_id, str) else event_id.decode()
                except WatchError:
                    continue

    async def read(self, stream: str, after: Optional[str], block_s: float) -> List[Event]:
        key = self._prefix + stream
        block = int(block_s * 1000) if block_s > 0 else None
        response = await self._redis.xread({key: after or "0-0"}, count=200, block=block)
        events = []
        for _key, entries in response or []:
            for event_id, fields in entries:
                events.append((event_id, fields["type"], json.loads(fields["data"])))
        return events


class EventPublisher:
    """Bound to one stream; tolerant of bus failures so telemetry never breaks a request."""

    def __init__(self, bus, stream: str, started: float):
        self.bus, self.stream, self.started = bus, stream, started
        self.first_event_s: Optional[float] = None

    async def __call__(self, event: str, data: Optional[Dict[str, Any]] = None) -> None:
        if self.first_event_s is None and event != "status":
            self.first_event_s = time.monotonic() - self.started
        try:
            await self.bus.publish(self.stream, event, {**(data or {}), "elapsed_ms": round((time.monotonic() - self.started) * 1000, 1)})
        except Exception:
            import logging
            logging.getLogger(__name__).warning("event publish failed for %s", event, exc_info=True)
