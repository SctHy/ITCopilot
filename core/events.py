"""Execution event log for live SSE.

Producers (API turn, orchestrator, tools, workflow worker) append events while work is
in progress; SSE subscribers read them as they arrive. Events are persisted per stream
(Redis Streams in deployments) so a client that reconnects with ``Last-Event-ID``
resumes after the last event it saw instead of re-running the request.

Payloads are redacted and size-capped: they are status records, not transcripts.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from core.redaction import redact

TERMINAL_EVENTS = frozenset({"completed", "error"})
STREAM_TTL_S = 3600
MAX_EVENTS = 2000
MAX_PAYLOAD_CHARS = 16000

Event = Tuple[str, str, Dict[str, Any]]  # (id, type, data)


def _encode(data: Dict[str, Any]) -> str:
    text = json.dumps(redact(data), ensure_ascii=False, default=str)
    if len(text) > MAX_PAYLOAD_CHARS:
        text = json.dumps({"truncated": True, "keys": sorted(data)[:50]}, ensure_ascii=False)
    return text


class InMemoryEventBus:
    """Single-process fallback (tests, Redis-less dev). Bounded number of streams."""

    def __init__(self, max_streams: int = 1000):
        self._streams: "OrderedDict[str, List[Tuple[str, str, str]]]" = OrderedDict()
        self._owners: Dict[str, str] = {}
        self._cond = asyncio.Condition()
        self._max_streams = max_streams
        self._seq = 0

    async def claim(self, stream: str, owner: str) -> bool:
        """Register the stream owner; False if the stream already exists (duplicate request)."""
        async with self._cond:
            if stream in self._owners:
                return False
            self._owners[stream] = owner
            self._streams[stream] = []
            while len(self._streams) > self._max_streams:
                old, _ = self._streams.popitem(last=False)
                self._owners.pop(old, None)
            return True

    async def owner(self, stream: str) -> Optional[str]:
        return self._owners.get(stream)

    async def publish(self, stream: str, event: str, data: Dict[str, Any]) -> str:
        async with self._cond:
            self._seq += 1
            event_id = f"{int(time.time() * 1000)}-{self._seq}"
            events = self._streams.setdefault(stream, [])
            events.append((event_id, event, _encode(data)))
            del events[:-MAX_EVENTS]
            self._cond.notify_all()
            return event_id

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

    async def claim(self, stream: str, owner: str) -> bool:
        return bool(await self._redis.set(f"{self._prefix}owner:{stream}", owner, nx=True, ex=STREAM_TTL_S))

    async def owner(self, stream: str) -> Optional[str]:
        return await self._redis.get(f"{self._prefix}owner:{stream}")

    async def publish(self, stream: str, event: str, data: Dict[str, Any]) -> str:
        key = self._prefix + stream
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.xadd(key, {"type": event, "data": _encode(data)}, maxlen=MAX_EVENTS, approximate=True)
            pipe.expire(key, STREAM_TTL_S)
            event_id, _ = await pipe.execute()
        return event_id if isinstance(event_id, str) else event_id.decode()

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
