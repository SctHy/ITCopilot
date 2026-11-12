"""Data retention: delete operational copies of personal text once they have served their purpose.

What is swept, and why it is safe:

- ``channel_inbox`` rows that are ``done`` / ``ignored``. They hold the raw IM message text.
  Their primary key de-duplicates platform retries, which stop within hours, and approval
  notices find the conversation through ``request_id``; so a row is kept while an approval
  it produced is still open (draft, pending, or approved and not finished executing).
- ``channel_outbox`` rows that are ``sent`` / ``dropped``. Delivery order only matters among
  rows still waiting, and a notice is queued once, in the transaction that settled it.
- ``vector_documents`` in the ``episodic`` collection (past conversation turns recalled into
  later prompts). The user profile collection is merged in place and is not swept.

What is never swept: approvals, decisions, actions, grants and the audit log are the record
of who changed what and are kept for audit. Failed inbox/outbox rows are kept because they
are what an operator is asked to look at. Shared-task transcripts are business records with
their own archive flow. Redis keys (working memory, receipts, traces, event streams) already
carry TTLs.

Deletion runs in bounded batches so a large backlog never holds a long write lock.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional

from sqlalchemy import and_, delete, exists, or_, select

from core.db import approvals, channel_inbox, channel_outbox, vector_documents
from core.env import env_float

logger = logging.getLogger(__name__)

DAY = 86400.0
# Terminal states as defined in intake/im.py (InboxStatus / OutboxStatus); failed rows are kept.
INBOX_SWEPT = ("done", "ignored")
OUTBOX_SWEPT = ("sent", "dropped")
EPISODIC_COLLECTION = "episodic"


@dataclass(frozen=True)
class RetentionPolicy:
    channel_days: Optional[float] = 30.0     # None: keep forever
    episodic_days: Optional[float] = 90.0
    batch: int = 500

    @classmethod
    def from_env(cls) -> "RetentionPolicy":
        def days(name: str, default: float) -> Optional[float]:
            value = env_float(name, default)
            return value if value > 0 else None
        return cls(channel_days=days("IT_COPILOT_RETENTION_CHANNEL_DAYS", 30.0),
                   episodic_days=days("IT_COPILOT_RETENTION_EPISODIC_DAYS", 90.0))


def _open_approval_for(request_id_column):
    still_open = or_(approvals.c.status.in_(("draft", "pending")),
                     and_(approvals.c.status == "approved", approvals.c.execution.in_(("queued", "executing", "manual_review"))))
    return exists().where(approvals.c.request_id == request_id_column, still_open)


class RetentionSweeper:
    def __init__(self, engine, policy: Optional[RetentionPolicy] = None, clock: Callable[[], float] = time.time,
                 interval_s: float = 3600.0):
        self.engine, self.policy, self.clock, self.interval_s = engine, policy or RetentionPolicy(), clock, interval_s
        self._task: Optional[asyncio.Task] = None

    def sweep(self) -> Dict[str, int]:
        """One full pass; returns rows deleted per kind."""
        now, report = self.clock(), {"channel_inbox": 0, "channel_outbox": 0, "episodic_memory": 0}
        if self.policy.channel_days:
            cutoff = now - self.policy.channel_days * DAY
            report["channel_inbox"] = self._drain(channel_inbox, channel_inbox.c.id, and_(
                channel_inbox.c.status.in_(INBOX_SWEPT), channel_inbox.c.updated_at < cutoff,
                or_(channel_inbox.c.request_id.is_(None), ~_open_approval_for(channel_inbox.c.request_id))))
            report["channel_outbox"] = self._drain(channel_outbox, channel_outbox.c.id, and_(
                channel_outbox.c.status.in_(OUTBOX_SWEPT), channel_outbox.c.created_at < cutoff))
        if self.policy.episodic_days:
            cutoff = now - self.policy.episodic_days * DAY
            report["episodic_memory"] = self._drain(vector_documents, vector_documents.c.id, and_(
                vector_documents.c.collection == EPISODIC_COLLECTION, vector_documents.c.updated_at < cutoff))
        if any(report.values()):
            from monitor import metrics
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

    # ── background loop ──────────────────────────────────────────────────

    def start(self) -> None:
        if self._task is None and (self.policy.channel_days or self.policy.episodic_days):
            self._task = asyncio.create_task(self._loop(), name="retention-sweeper")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.sweep)
            except Exception:
                logger.exception("retention sweep failed")
            await asyncio.sleep(self.interval_s)
