"""Lease-fenced immutable checkpoints. Agent code uses only the scoped async port."""
import asyncio
from sqlalchemy import select, insert
from core.db import task_checkpoints, task_runs
from core.concurrency import LeaseLostError
from workflow.tasks import key, TaskConflict


class TaskPlanStore:
    def __init__(self, tasks):
        self.tasks = tasks

    def bind(self, claim):
        return CheckpointSession(self, claim)

    def _require(self, db, task, claim):
        if task['status'] != 'open' or not db.execute(select(task_runs.c.id).where(self.tasks._owned(claim))).first():
            raise LeaseLostError('检查点所属运行租约已失效')

    def read(self, claim, name, inputs):
        with self.tasks.transaction(claim['task_id']) as (db, task):
            self._require(db, task, claim)
            row = db.execute(select(task_checkpoints).where(task_checkpoints.c.run_id == claim['id'], task_checkpoints.c.step_key == name)).mappings().first()
            if row and row['input_hash'] != key(inputs):
                raise TaskConflict('检查点输入已变化，拒绝复用旧结果')
            return row['payload'] if row else None

    def save(self, claim, name, inputs, payload, usage):
        with self.tasks.transaction(claim['task_id']) as (db, task):
            self._require(db, task, claim)
            row = db.execute(select(task_checkpoints).where(task_checkpoints.c.run_id == claim['id'], task_checkpoints.c.step_key == name)).mappings().first()
            if row:
                if row['input_hash'] != key(inputs) or row['payload'] != payload:
                    raise TaskConflict('不能覆盖已完成的检查点')
                return
            db.execute(insert(task_checkpoints).values(run_id=claim['id'], step_key=name, task_id=task['id'],
                input_hash=key(inputs), payload=payload, usage=usage, created_at=self.tasks.clock()))
            if self.tasks.governance:
                self.tasks.governance.charge(db, claim, usage)
            self._require(db, task, claim)

    def list(self, task_id, principal):
        with self.tasks.read(task_id, principal) as (db, _):
            # Payloads can contain tool data; the shared UI sees only checkpoint metadata.
            columns = [c for c in task_checkpoints.c if c.name != 'payload']
            return [dict(r) for r in db.execute(select(*columns).where(task_checkpoints.c.task_id == task_id)
                .order_by(task_checkpoints.c.created_at.desc()).limit(100)).mappings()]


class CheckpointSession:
    def __init__(self, store, claim):
        self.store, self.claim = store, dict(claim)

    @staticmethod
    async def _call(fn, *args):
        operation = asyncio.create_task(asyncio.to_thread(fn, *args))
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            await asyncio.gather(operation, return_exceptions=True)
            raise

    async def read(self, name, inputs):
        return await self._call(self.store.read, self.claim, name, inputs)

    async def save(self, name, inputs, payload, usage=None):
        await self._call(self.store.save, self.claim, name, inputs, payload, usage or {})

    async def failed(self, name, inputs, payload, usage):
        await self.save('failed/' + name + '/' + str(self.claim['version']), inputs, payload, usage)
