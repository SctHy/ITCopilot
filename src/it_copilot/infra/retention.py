"""Expire redacted chat history, turn metadata and episodic memory in bounded batches."""
from __future__ import annotations

import asyncio
import logging
import os
import math
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional

from sqlalchemy import and_, delete, select

from it_copilot.infra.db import conversation_messages, conversations, vector_documents, turn_records
from it_copilot.infra.env import env_float

logger = logging.getLogger(__name__)

DAY = 86400.0
EPISODIC_COLLECTION = "episodic"


@dataclass(frozen=True)
class RetentionPolicy:
    episodic_days: Optional[float] = 90.0
    turn_days: Optional[float] = 90.0
    history_days: Optional[float] = 90.0
    batch: int = 500

    @classmethod
    def from_env(cls) -> "RetentionPolicy":
        def days(name: str, default: float) -> Optional[float]:
            value = env_float(name, default)
            return value if value > 0 else None
        raw_turn_days = float(os.getenv("IT_COPILOT_RETENTION_TURN_DAYS", str(cls.turn_days)))
        if not math.isfinite(raw_turn_days) or raw_turn_days < 0:
            raise ValueError("IT_COPILOT_RETENTION_TURN_DAYS must be a finite nonnegative number")
        return cls(episodic_days=days("IT_COPILOT_RETENTION_EPISODIC_DAYS", 90.0),
                   turn_days=raw_turn_days if raw_turn_days > 0 else None,
                   history_days=days("IT_COPILOT_RETENTION_HISTORY_DAYS", 90.0))


class RetentionSweeper:
    def __init__(self, engine, policy: Optional[RetentionPolicy] = None, clock: Callable[[], float] = time.time,
                 interval_s: float = 3600.0, *, database=None):
        self._engine, self.policy, self.clock, self.interval_s = engine, policy or RetentionPolicy(), clock, interval_s
        self.database = database
        self._task: Optional[asyncio.Task] = None

    @property
    def engine(self):
        return self.database.engine if self.database else self._engine

    async def sweep_async(self):
        if self.database:
            return await self.database.run(self.sweep)
        return await asyncio.to_thread(self.sweep)

    def sweep(self) -> Dict[str, int]:
        """One full pass; returns rows deleted per kind."""
        now, report = self.clock(), {"episodic_memory": 0}
        if self.policy.episodic_days:
            cutoff = now - self.policy.episodic_days * DAY
            report["episodic_memory"] = self._drain(vector_documents, vector_documents.c.id, and_(
                vector_documents.c.collection == EPISODIC_COLLECTION, vector_documents.c.updated_at < cutoff))
        if self.policy.turn_days:
            report["turn_records"] = self._drain(turn_records, turn_records.c.request_id,
                turn_records.c.created_at < now - self.policy.turn_days * DAY)
        if self.policy.history_days:
            cutoff = now - self.policy.history_days * DAY
            report["chat_history"] = self._drain(conversation_messages, conversation_messages.c.id,
                conversation_messages.c.created_at < cutoff)
            report["chat_conversations"] = self._drain_conversations(conversations.c.updated_at < cutoff)
        if any(report.values()):
            from it_copilot.infra.monitor import metrics
            for kind, count in report.items():
                if count:
                    metrics.RETENTION_DELETED.labels(kind=kind).inc(count)
            logger.info("retention sweep deleted %s", report)
        return report

    def _drain(self, table, key, condition) -> int:
        total = 0
        while True:
            with self.engine.begin() as db:
                ids = db.execute(select(key).where(condition).limit(self.policy.batch)).scalars().all()
                if not ids:
                    return total
                # Re-apply the condition: a row that changed since the select is left alone, and for
                # vector_documents it also keeps the (collection, id) key inside the episodic collection.
                deleted = db.execute(delete(table).where(key.in_(ids), condition)).rowcount
            total += deleted
            if len(ids) < self.policy.batch:
                return total

    def _drain_conversations(self, condition) -> int:
        # Composite key: delete one bounded batch at a time, re-checking the condition so a conversation
        # that received a new message since the select is kept.
        total = 0
        while True:
            with self.engine.begin() as db:
                keys = db.execute(select(conversations.c.tenant, conversations.c.owner, conversations.c.conv_id)
                                  .where(condition).limit(self.policy.batch)).all()
                for tenant, owner, conv_id in keys:
                    total += db.execute(delete(conversations).where(
                        conversations.c.tenant == tenant, conversations.c.owner == owner,
                        conversations.c.conv_id == conv_id, condition)).rowcount or 0
            if len(keys) < self.policy.batch:
                return total

    # ── background loop ──────────────────────────────────────────────────

    def start(self) -> None:
        if self._task is None and (self.policy.episodic_days or self.policy.turn_days
                                   or self.policy.history_days):
            self._task = asyncio.create_task(self._loop(), name="retention-sweeper")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.sweep_async()
            except Exception:
                logger.exception("retention sweep failed")
            await asyncio.sleep(self.interval_s)
