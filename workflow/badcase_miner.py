"""Freeze short-lived traces before expiry; mine metadata and business outcomes incrementally."""
import asyncio
import hashlib
import time

import numpy as np
from sqlalchemy import or_, select, update
from sqlalchemy.dialects import postgresql, sqlite

from agents.outcome import turn_outcome
from core.db import (approvals, badcase_mining_cursors, badcases, feedback, read,
                     service_tasks, task_events, task_verifications, turn_records)
from core.embedding import Embedder
from core.redaction import redact
from core.tenant_context import system_scope


class BadcaseMiner:
    def __init__(self, engine, memory=None, *, embedder=None, clock=time.time, interval_s=600):
        self.engine, self.memory, self.clock, self.interval_s = engine, memory, clock, interval_s
        self.embedder = Embedder() if embedder is None else embedder
        self._task = None

    def _candidates(self, now):
        with system_scope(), self.engine.begin() as db:
            statement = (postgresql if db.dialect.name == "postgresql" else sqlite).insert(badcase_mining_cursors)
            db.execute(statement.values(name="main", at=0).on_conflict_do_nothing(index_elements=[badcase_mining_cursors.c.name]))
            since = db.scalar(select(badcase_mining_cursors.c.at).where(badcase_mining_cursors.c.name == "main"))
            recent_feedback = select(feedback.c.request_id).where(feedback.c.created_at >= since)
            changed_approvals = select(approvals.c.request_id).where(or_(approvals.c.decided_at >= since,
                                                                        approvals.c.expires_at.between(since, now)))
            changed_tasks = select(service_tasks.c.id).where(service_tasks.c.updated_at >= since)
            query = select(turn_records).where(or_(turn_records.c.created_at >= since - self.interval_s,
                turn_records.c.request_id.in_(recent_feedback), turn_records.c.request_id.in_(changed_approvals),
                turn_records.c.task_id.in_(changed_tasks))).order_by(turn_records.c.created_at, turn_records.c.request_id)
            rows = [dict(r) for r in db.execute(query).mappings()]
            negatives = set(db.scalars(select(feedback.c.request_id).where(feedback.c.rating == -1)))
        return rows, negatives, since

    def _business_signals(self, row):
        signals = set(row["signals"])
        with read(self.engine) as db:
            if row["approval_id"] is not None:
                approval = db.execute(select(approvals).where(approvals.c.id == row["approval_id"],
                                                              approvals.c.tenant == row["tenant"])).mappings().one()
                if approval["status"] == "rejected":
                    signals.add("approval_rejected")
                if approval["status"] in {"withdrawn", "expired"} and approval["confirmed_at"] is None:
                    signals.add("draft_withdrawn")
            if row["task_id"] is not None:
                checks = db.scalars(select(task_verifications.c.status).where(task_verifications.c.task_id == row["task_id"]))
                if "failed" in checks:
                    signals.add("verification_failed")
                reopened = db.scalar(select(task_events.c.id).where(task_events.c.task_id == row["task_id"],
                    task_events.c.kind == "progress", task_events.c.text == "重新打开事项，等待后续处理。").limit(1))
                if reopened is not None:
                    signals.add("task_reopened")
        return signals

    async def _snapshot(self, row):
        trace = None
        turns, response, missing_slots = [], "", []
        if row["task_id"] is not None:
            with read(self.engine) as db:
                user = db.scalar(select(task_events.c.text).where(task_events.c.id == row["request_id"], task_events.c.task_id == row["task_id"]))
                assistant = db.scalar(select(task_events.c.text).where(task_events.c.task_id == row["task_id"],
                    task_events.c.kind == "assistant", task_events.c.data["run_id"].as_string() == row["request_id"]).limit(1))
            if user is not None and assistant is not None:
                turns, response = [user], assistant
        elif self.memory is not None:
            stored = await self.memory.turn_snapshot(row["owner"], row["conv_id"], row["request_id"])
            trace, turns, response, missing_slots = stored["trace"], stored["turns"], stored["response"], stored["missing_slots"]
        return redact({"turn_record": row, "trace": trace, "turns": turns, "response": response,
                       "missing_slots": missing_slots, "snapshot_partial": not bool(turns and response)})

    def _save(self, row, signals, source, snapshot):
        with self.engine.begin() as db:
            db.execute(update(turn_records).where(turn_records.c.request_id == row["request_id"],
                turn_records.c.tenant == row["tenant"]).values(signals=sorted(signals)))
            statement = (postgresql if db.dialect.name == "postgresql" else sqlite).insert(badcases)
            case_id = hashlib.sha256(row["request_id"].encode()).hexdigest()
            db.execute(statement.values(id=case_id, request_id=row["request_id"], tenant=row["tenant"], source=source,
                signals=sorted(signals), status="new", snapshot=snapshot, label={}).on_conflict_do_nothing(index_elements=[badcases.c.request_id]))
            existing = db.execute(select(badcases).where(badcases.c.request_id == row["request_id"], badcases.c.tenant == row["tenant"])
                                  .with_for_update()).mappings().one()
            values = {"signals": sorted(set(existing["signals"]) | signals)}
            if existing["snapshot"]["snapshot_partial"] and not snapshot["snapshot_partial"]:
                values["snapshot"] = snapshot
            if source == "feedback" and existing["status"] == "new":
                values["source"] = "feedback"
            db.execute(update(badcases).where(badcases.c.id == case_id, badcases.c.tenant == row["tenant"]).values(**values))

    async def run_once(self):
        now = self.clock()
        with system_scope():
            rows, negatives, since = await asyncio.to_thread(self._candidates, now)
            snapshots, counts = {}, {"feedback": 0, "signal": 0, "sampled": 0}
            for row in rows:
                if now - row["created_at"] <= 86400:
                    snapshots[row["request_id"]] = await self._snapshot(row)
            previous = {}
            for row in rows:
                signals = await asyncio.to_thread(self._business_signals, row)
                snapshot = snapshots[row["request_id"]] if row["request_id"] in snapshots else await self._snapshot(row)
                key = (row["tenant"], row["owner"], row["conv_id"])
                old = previous.get(key)
                clarify_count = (old[2] if old is not None else 0) + 1 if snapshot["missing_slots"] else 0
                if clarify_count >= 3:
                    signals.add("clarify_loop")
                if old is not None and row["created_at"] - old[0]["created_at"] <= 60 and snapshot["turns"] and old[1]["turns"]:
                    vectors = await asyncio.to_thread(self.embedder.embed_documents, [old[1]["turns"][0], snapshot["turns"][0]])
                    if 1 - float(np.dot(vectors[0], vectors[1])) < .15:
                        # The preceding answer triggered the user's rephrase, so label that turn.
                        prior = old[0]
                        await asyncio.to_thread(self._save, prior, set(prior["signals"]) | {"rephrase"}, "signal", old[1])
                previous[key] = (row, snapshot, clarify_count)
                source = "feedback" if row["request_id"] in negatives else "signal" if signals else "sampled"
                sampled = int(hashlib.sha256(row["request_id"].encode()).hexdigest(), 16) % 100 == 0
                if source != "sampled" or (turn_outcome(row) == "success" and sampled):
                    await asyncio.to_thread(self._save, row, signals, source, snapshot)
                    counts[source] += 1
            with self.engine.begin() as db:
                db.execute(update(badcase_mining_cursors).where(badcase_mining_cursors.c.name == "main",
                    badcase_mining_cursors.c.at == since).values(at=now))
            return counts

    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="badcase-miner")

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self):
        while True:
            await self.run_once()
            await asyncio.sleep(self.interval_s)


class RedisTurnSnapshots:
    """The execution-only worker reads trace copies without constructing model clients."""
    def __init__(self, redis_client):
        self.redis = redis_client

    async def turn_snapshot(self, owner, conv_id, request_id):
        import json
        raw = await self.redis.get("trace:" + request_id)
        if raw is None:
            return {"trace": None, "turns": [], "response": "", "missing_slots": []}
        trace = json.loads(raw)
        if "turn" not in trace:
            return {"trace": trace, "turns": [], "response": "", "missing_slots": []}
        turn = trace["turn"]
        return {"trace": trace, "turns": [turn["user"]], "response": turn["assistant"], "missing_slots": turn["missing_slots"]}
