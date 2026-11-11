"""Admission budgets and attempt ledger. Token limits gate the NEXT run, not provider billing."""
from sqlalchemy import insert, select, update
from core.db import task_budgets, task_attempts, task_events, task_runs
from workflow.tasks import TaskForbidden, key


class TaskGovernance:
    def __init__(self, tasks, token_limit=200000, run_limit=30):
        self.tasks, self.token_limit, self.run_limit = tasks, token_limit, run_limit

    def _budget(self, db, task_id):
        row = db.execute(select(task_budgets).where(task_budgets.c.task_id == task_id)).mappings().first()
        return dict(row) if row else dict(task_id=task_id, token_limit=self.token_limit, run_limit=self.run_limit, used_tokens=0, used_runs=0)

    def admit(self, db, task, claim):
        now = self.tasks.clock()
        db.execute(update(task_attempts).where(task_attempts.c.run_id == claim['id'], task_attempts.c.status == 'running')
                   .values(status='interrupted', ended_at=now, error='lease_expired_usage_unknown'))
        budget = self._budget(db, task['id'])
        if budget['used_tokens'] >= budget['token_limit'] or budget['used_runs'] >= budget['run_limit']:
            db.execute(update(task_runs).where(task_runs.c.id == claim['id']).values(status='held', attempts=claim['attempts'] - 1, lease_until=None, error='budget_exhausted'))
            return False
        exists = db.execute(select(task_budgets.c.task_id).where(task_budgets.c.task_id == task['id'])).first()
        budget['used_runs'] += 1
        if exists:
            db.execute(update(task_budgets).where(task_budgets.c.task_id == task['id']).values(**budget))
        else:
            db.execute(insert(task_budgets).values(**budget))
        queued = db.execute(select(task_events.c.created_at).where(task_events.c.id == claim['id'])).scalar()
        db.execute(insert(task_attempts).values(id=key(claim['id'], claim['version']), task_id=task['id'], run_id=claim['id'],
            attempt=claim['attempts'], queued_at=queued, started_at=now, status='running', usage={}, tools=[]))
        return True

    def complete(self, db, claim, result=None, error=None):
        usage = (result or {}).get('token_usage', {})
        if (result or {}).get('usage_checkpointed') or error:
            row = db.execute(select(task_attempts.c.usage).where(task_attempts.c.id == key(claim['id'], claim['version']))).scalar()
            usage = row or {}
        else:
            self.charge(db, claim, usage)
        # Do not expose arbitrary tool output or credentials to shared participants.
        safe_tools = [{k: t[k] for k in ('tool_name', 'success', 'latency_ms') if k in t} for t in (result or {}).get('tool_traces', [])]
        db.execute(update(task_attempts).where(task_attempts.c.id == key(claim['id'], claim['version']))
                   .values(ended_at=self.tasks.clock(), status='failed' if error else 'done', error=error, usage=usage, tools=safe_tools))

    def charge(self, db, claim, usage):
        total = max(0, int(usage.get('input_tokens', 0))) + max(0, int(usage.get('output_tokens', 0)))
        if not total:
            total = max(0, int(usage.get('total_tokens', 0)))
        db.execute(update(task_budgets).where(task_budgets.c.task_id == claim['task_id']).values(used_tokens=task_budgets.c.used_tokens + total))
        old = db.execute(select(task_attempts.c.usage).where(task_attempts.c.id == key(claim['id'], claim['version']))).scalar() or {}
        accumulated = {k: old.get(k, 0) + max(0, int(v)) for k, v in usage.items()}
        accumulated = {**old, **accumulated}
        db.execute(update(task_attempts).where(task_attempts.c.id == key(claim['id'], claim['version']))
                   .values(usage=accumulated))

    def interrupt(self, db, run_ids, reason):
        db.execute(update(task_attempts).where(task_attempts.c.run_id.in_(run_ids), task_attempts.c.status == 'running')
                   .values(status='interrupted', ended_at=self.tasks.clock(), error=reason))

    def read(self, task_id, principal):
        with self.tasks.read(task_id, principal) as (db, task):
            attempts = [dict(r) for r in db.execute(select(task_attempts).where(task_attempts.c.task_id == task_id)
                        .order_by(task_attempts.c.started_at.desc()).limit(100)).mappings()]
            return {'budget': self._budget(db, task_id), 'attempts': attempts}

    def configure(self, task_id, principal, token_limit, run_limit):
        with self.tasks.transaction(task_id, principal) as (db, task):
            if not principal.is_admin:
                raise TaskForbidden('只有 IT 管理员可以调整累计预算')
            budget = self._budget(db, task_id)
            exists = db.execute(select(task_budgets.c.task_id).where(task_budgets.c.task_id == task_id)).first()
            budget.update(token_limit=token_limit, run_limit=run_limit)
            if exists:
                db.execute(update(task_budgets).where(task_budgets.c.task_id == task_id).values(**budget))
            else:
                db.execute(insert(task_budgets).values(**budget))
            db.execute(update(task_runs).where(task_runs.c.task_id == task_id, task_runs.c.status == 'held').values(status='queued'))
            self.tasks.record(db, task, principal, 'budget', '管理员调整了任务累计预算。', {'token_limit': token_limit, 'run_limit': run_limit})
