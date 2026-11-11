"""Durable independent verification, bounded read-only follow-ups, and acceptance gate."""
import asyncio
import json
import logging
from sqlalchemy import select, insert, update, or_, and_

from core.db import task_goals, task_verifications as jobs, task_progress, task_events, task_runs
from core.concurrency import LeaseLostError
from workflow.tasks import TaskConflict, TaskForbidden, key

logger = logging.getLogger(__name__)
FRESH_SECONDS = 300


class TaskGoalService:
    def __init__(self, tasks, verifier, ready=lambda db, task: True):
        self.tasks, self.verifier, self.ready = tasks, verifier, ready

    @staticmethod
    def _goal(db, task_id):
        row = db.execute(select(task_goals).where(task_goals.c.task_id == task_id)).mappings().first()
        return dict(row) if row else dict(task_id=task_id, version=0, checks=[], replan_limit=2, replans_used=0, latest_id=None)

    @staticmethod
    def _progress(db, task_id):
        return db.execute(select(task_progress.c.version).where(task_progress.c.task_id == task_id)).scalar() or 0

    @staticmethod
    def _busy(db, task):
        return db.execute(select(task_runs.c.id).where(task_runs.c.task_id == task['id'],
            task_runs.c.status.in_(['queued', 'running', 'held']))).first() is not None

    def read(self, task_id, principal):
        with self.tasks.read(task_id, principal) as (db, task):
            history = [dict(r) for r in db.execute(select(jobs).where(jobs.c.task_id == task_id)
                .order_by(jobs.c.created_at.desc(), jobs.c.id).limit(20)).mappings()]
            return {'goal': self._goal(db, task_id), 'history': history, 'catalog': self.verifier.catalog(),
                    'acceptable': self.can_resolve(db, task), 'fresh_seconds': FRESH_SECONDS}

    def configure(self, task_id, principal, version, checks, replan_limit):
        checks = self.verifier.normalize(checks)
        if type(replan_limit) is not int or not 0 <= replan_limit <= 2:
            raise TaskConflict('自动只读重规划上限为 0 到 2 轮')
        with self.tasks.transaction(task_id, principal) as (db, task):
            if task['requester'] != principal.subject and not principal.is_admin:
                raise TaskForbidden('只有申请人或 IT 管理员可以修改核验目标')
            if task['status'] != 'open':
                raise TaskConflict('请先重新打开事项')
            old = self._goal(db, task_id)
            if old['version'] != version:
                raise TaskConflict('核验目标已变化，请刷新后重试')
            values = dict(version=version + 1, checks=checks, replan_limit=replan_limit, replans_used=0, latest_id=None)
            if old['version']:
                db.execute(update(task_goals).where(task_goals.c.task_id == task_id).values(**values))
            else:
                db.execute(insert(task_goals).values(task_id=task_id, **values))
            self.cancel(db, task)
            # Superseded follow-ups cannot consume resources or commit against a new goal.
            followups = [r['id'] for r in db.execute(select(task_events).where(task_events.c.task_id == task_id,
                task_events.c.kind == 'verification')).mappings() if r['data'].get('replan')]
            db.execute(update(task_runs).where(task_runs.c.id.in_(followups), task_runs.c.status.in_(['queued', 'running', 'held']))
                .values(status='cancelled', version=task_runs.c.version + 1, lease_until=None))
            if self.tasks.governance:
                self.tasks.governance.interrupt(db, followups, 'goal_changed_usage_unknown')
            self.tasks.record(db, task, principal, 'goal', '更新了独立核验条件。', values)
            self._enqueue(db, task, key('goal', task_id, version + 1))

    def request(self, task_id, principal, client_id):
        with self.tasks.transaction(task_id, principal) as (db, task):
            if task['status'] != 'open' or not self._goal(db, task_id)['checks']:
                raise TaskConflict('请在打开的事项中配置核验条件')
            return self._enqueue(db, task, key('verify', task_id, principal.subject, client_id))

    def _enqueue(self, db, task, job_id):
        goal = self._goal(db, task['id'])
        if task['status'] != 'open' or not goal['checks']:
            return None
        old = db.execute(select(jobs).where(jobs.c.id == job_id)).mappings().first()
        if old:
            return old['id']
        self.cancel(db, task, 'stale')
        db.execute(insert(jobs).values(id=job_id, task_id=task['id'], goal_version=goal['version'],
            progress_version=self._progress(db, task['id']), checks=goal['checks'], status='queued',
            version=0, attempts=0, created_at=self.tasks.clock(), observations=[]))
        db.execute(update(task_goals).where(task_goals.c.task_id == task['id']).values(latest_id=job_id))
        return job_id

    def view(self, task):
        return {}

    def commit(self, db, task, event, claim, result):
        self._enqueue(db, task, key('verify-run', claim['id']))
        return {}

    def cancel(self, db, task, status='cancelled'):
        db.execute(update(jobs).where(jobs.c.task_id == task['id'], jobs.c.status.in_(['queued', 'running']))
            .values(status=status, version=jobs.c.version + 1, lease_until=None, ended_at=self.tasks.clock()))

    def can_resolve(self, db, task):
        goal = self._goal(db, task['id'])
        if not goal['checks']:
            return True
        # Any new check invalidates earlier acceptance evidence, even if it is pending or failed.
        if self._busy(db, task) or not self.ready(db, task):
            return False
        row = db.execute(select(jobs).where(jobs.c.id == goal['latest_id'],
            jobs.c.goal_version == goal['version'], jobs.c.progress_version == self._progress(db, task['id']))).mappings().first()
        return bool(row and row['status'] == 'passed' and row['ended_at'] >= self.tasks.clock() - FRESH_SECONDS)

    def claim(self, lease_s=60):
        now = self.tasks.clock()
        due = or_(jobs.c.status == 'queued', and_(jobs.c.status == 'running', jobs.c.lease_until <= now))
        with self.tasks.engine.connect() as db:
            candidates = [dict(r) for r in db.execute(select(jobs).where(due).order_by(jobs.c.created_at)).mappings()]
        for item in candidates:
            with self.tasks.transaction(item['task_id']) as (db, task):
                row = db.execute(select(jobs).where(jobs.c.id == item['id'], due)).mappings().first()
                if not row:
                    continue
                goal = self._goal(db, task['id'])
                if task['status'] != 'open' or row['goal_version'] != goal['version'] or row['progress_version'] != self._progress(db, task['id']):
                    db.execute(update(jobs).where(jobs.c.id == row['id']).values(status='stale', lease_until=None, ended_at=now))
                    continue
                if self._busy(db, task) or not self.ready(db, task):
                    continue  # waiting on a human/approval consumes neither a lease nor a replan
                if row['attempts'] >= 3:
                    db.execute(update(jobs).where(jobs.c.id == row['id']).values(status='unknown', lease_until=None,
                        ended_at=now, observations=[{'status': 'unknown', 'reason': '核验多次中断，需要人工核查'}]))
                    self.tasks.record(db, task, None, 'verification', '核验多次中断，需要人工核查。')
                    continue
                claim = {**dict(row), 'status': 'running', 'version': row['version'] + 1,
                         'attempts': row['attempts'] + 1, 'lease_until': now + lease_s}
                db.execute(update(jobs).where(jobs.c.id == row['id']).values(**claim))
                return claim
        return None

    def context(self, claim):
        with self.tasks.read(claim['task_id']) as (_, task):
            return task

    def settle(self, claim, observations):
        with self.tasks.transaction(claim['task_id']) as (db, task):
            owned = and_(jobs.c.id == claim['id'], jobs.c.version == claim['version'], jobs.c.status == 'running', jobs.c.lease_until > self.tasks.clock())
            if not db.execute(select(jobs.c.id).where(owned)).first():
                return False
            goal = self._goal(db, task['id'])
            valid = (len(observations) == len(claim['checks']) and all(o.get('status') in ('passed', 'failed', 'unknown') for o in observations))
            status = 'unknown' if not valid or any(o.get('status') == 'unknown' for o in observations) else 'failed' if any(o['status'] == 'failed' for o in observations) else 'passed'
            stale = (goal['latest_id'] != claim['id'] or task['status'] != 'open' or goal['version'] != claim['goal_version']
                or self._progress(db, task['id']) != claim['progress_version'] or self._busy(db, task) or not self.ready(db, task))
            if stale:
                status = 'stale'
            changed = db.execute(update(jobs).where(owned).values(status=status, observations=observations,
                ended_at=self.tasks.clock(), lease_until=None)).rowcount
            if not changed or stale:
                return False
            replan = status == 'failed' and goal['replans_used'] < goal['replan_limit']
            if replan:
                db.execute(update(task_goals).where(task_goals.c.task_id == task['id'])
                    .values(replans_used=goal['replans_used'] + 1))
            message = ('独立核验通过，等待申请人验收。' if status == 'passed' else
                       '目标尚未达成，继续一轮只读诊断与重规划。' if replan else '目标未通过独立核验，需要人工跟进。')
            self.tasks.record(db, task, None, 'verification', message + '\n' + json.dumps(observations, ensure_ascii=False),
                {'verification_id': claim['id'], 'goal_version': goal['version'], 'status': status, 'replan': replan},
                wake=replan, event_id=key('verification-result', claim['id']))
            if claim['lease_until'] <= self.tasks.clock():
                raise LeaseLostError('核验提交时租约已失效')
            return True


class TaskVerificationWorker:
    """Dedicated bounded reader. It never shares the Agent's tool execution authority."""
    def __init__(self, service, poll_s=2, timeout_s=20):
        self.service, self.poll_s, self.timeout_s = service, poll_s, timeout_s
        self._task = None

    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name='goal-verification')

    async def stop(self):
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self):
        while True:
            try:
                if await self.run_once():
                    continue
            except Exception:
                logger.exception('goal verification iteration failed')
            await asyncio.sleep(self.poll_s)

    async def run_once(self):
        claim = await asyncio.to_thread(self.service.claim)
        if not claim:
            return False
        task = await asyncio.to_thread(self.service.context, claim)
        try:
            observations = await asyncio.wait_for(self.service.verifier.verify(task, claim['checks']), self.timeout_s)
        except Exception:
            observations = [{**c, 'status': 'unknown', 'reason': '核验不可用或超时，需要人工核查'} for c in claim['checks']]
        await asyncio.to_thread(self.service.settle, claim, observations)
        return True
