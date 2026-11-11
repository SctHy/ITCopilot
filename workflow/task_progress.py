"""Versioned progress and human acceptance. Independent of approval internals."""
from contextlib import nullcontext
from sqlalchemy import insert, select, update
from core.db import task_progress, task_events
from workflow.tasks import TaskConflict, TaskForbidden


class TaskProgressService:
    def __init__(self, tasks, can_resolve=lambda db, task: True):
        self.tasks, self.can_resolve = tasks, can_resolve

    def read(self, task_id, principal=None):
        with self.tasks.read(task_id, principal) as (db, task):
            row = db.execute(select(task_progress).where(task_progress.c.task_id == task_id)).mappings().first()
            return dict(row) if row else dict(task_id=task_id, version=0, goal=task['title'], steps=[], blocker='', next_action='', resolution='')

    def context(self, task_id):
        progress = self.read(task_id)
        ids = list(dict.fromkeys(e for step in progress['steps'] for e in step['evidence_ids']))[:30]
        with self.tasks.read(task_id) as (db, task):
            rows = db.execute(select(task_events).where(task_events.c.task_id == task_id, task_events.c.id.in_(ids))).mappings()
            progress['evidence'] = [{'id': r['id'], 'kind': r['kind'], 'actor_name': r['actor_name'], 'text': r['text'][:1500]} for r in rows]
        return progress

    def save(self, task_id, principal, payload, transaction=None):
        with (nullcontext(transaction) if transaction else self.tasks.transaction(task_id, principal)) as (db, task):
            if task['status'] != 'open':
                raise TaskConflict('请先重新打开事项')
            row = db.execute(select(task_progress).where(task_progress.c.task_id == task_id)).mappings().first()
            version = row['version'] if row else 0
            if payload['version'] != version:
                raise TaskConflict('进度已被其他人更新，请刷新后重试')
            for step in payload['steps']:
                if step['status'] == 'done' and not step['evidence_ids']:
                    raise TaskConflict('已完成步骤需要引用事项中的证据消息')
                for event_id in step['evidence_ids']:
                    if not db.execute(select(task_events.c.id).where(task_events.c.id == event_id, task_events.c.task_id == task_id)).first():
                        raise TaskConflict('证据必须属于当前事项')
            values = {k: payload[k] for k in ('goal', 'steps', 'blocker', 'next_action')}
            values.update(version=version + 1, resolution='')
            if row:
                db.execute(update(task_progress).where(task_progress.c.task_id == task_id).values(**values))
            else:
                db.execute(insert(task_progress).values(task_id=task_id, **values))
            self.tasks.record(db, task, principal, 'progress', '更新了任务进度与完成标准。', {'progress_version': version + 1})
        return values if transaction else self.read(task_id, principal)

    def transition(self, task_id, principal, status, resolution, transaction=None):
        with (nullcontext(transaction) if transaction else self.tasks.transaction(task_id, principal)) as (db, task):
            if task['requester'] != principal.subject and not principal.is_admin:
                raise TaskForbidden('只有申请人或 IT 管理员可以验收或重新打开')
            if status == task['status']:
                return
            if status == 'resolved':
                if task['status'] != 'open':
                    raise TaskConflict('请先重新打开事项')
                row = db.execute(select(task_progress).where(task_progress.c.task_id == task_id)).mappings().first()
                if not row or not row['steps'] or row['blocker'] or any(s['status'] != 'done' for s in row['steps']):
                    raise TaskConflict('仍有未完成步骤或阻塞原因，不能验收')
                if not resolution.strip() or not self.can_resolve(db, task):
                    raise TaskConflict('需要验收说明，审批、执行及待办必须处理完毕；配置了核验条件时还需当前进度的有效核验结果')
                db.execute(update(task_progress).where(task_progress.c.task_id == task_id).values(resolution=resolution, version=row['version'] + 1))

            elif status != 'open':
                raise TaskConflict('不支持该状态迁移')
            if status == 'open':
                db.execute(update(task_progress).where(task_progress.c.task_id == task_id)
                           .values(resolution='', version=task_progress.c.version + 1))
            self.tasks.set_status(db, task, status)
            self.tasks.record(db, task, principal, 'progress', '已验收解决：' + resolution if status == 'resolved' else '重新打开事项，等待后续处理。')
