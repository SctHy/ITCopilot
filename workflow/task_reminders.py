"""Durable timers and overdue-question sources; delivery is a shared-room event."""
from sqlalchemy import insert, select, update
from core.db import task_timers, task_todos
from workflow.tasks import TaskConflict, TaskForbidden, key


class TaskReminderService:
    def __init__(self, tasks):
        self.tasks = tasks

    def list(self, task_id, principal):
        with self.tasks.read(task_id, principal) as (db, task):
            return [dict(r) for r in db.execute(select(task_timers).where(task_timers.c.task_id == task_id).order_by(task_timers.c.due_at)).mappings()]

    def create(self, task_id, principal, payload):
        with self.tasks.transaction(task_id, principal) as (db, task):
            if task['requester'] != principal.subject and not principal.is_admin:
                raise TaskForbidden('只有申请人或管理员可以安排提醒或复查')
            if task['status'] != 'open':
                raise TaskConflict('请先重新打开事项')
            timer_id = key(task_id, principal.subject, 'timer', payload['client_request_id'])
            old = db.execute(select(task_timers).where(task_timers.c.id == timer_id)).mappings().first()
            if old:
                if any(old[k] != payload[k] for k in ('text', 'due_at', 'wake')):
                    raise TaskConflict('同一请求键不能用于不同提醒')
                return dict(old)
            if payload['due_at'] <= self.tasks.clock():
                raise TaskConflict('提醒时间必须在未来')
            row = {k: payload[k] for k in ('text', 'due_at', 'wake')}
            row.update(id=timer_id, task_id=task_id, status='pending')
            db.execute(insert(task_timers).values(**row))
            self.tasks.record(db, task, principal, 'timer', '安排提醒：' + row['text'], {'timer_id': timer_id})
            return row

    def cancel(self, task_id, timer_id, principal):
        with self.tasks.transaction(task_id, principal) as (db, task):
            if task['requester'] != principal.subject and not principal.is_admin:
                raise TaskForbidden('只有申请人或管理员可以取消提醒')
            db.execute(update(task_timers).where(task_timers.c.id == timer_id, task_timers.c.task_id == task_id,
                task_timers.c.status == 'pending').values(status='cancelled'))

    def poll(self):
        with self.tasks.engine.connect() as db:
            due = db.execute(select(task_timers).where(task_timers.c.status == 'pending', task_timers.c.due_at <= self.tasks.clock()).limit(100)).mappings().all()
        for candidate in due:
            with self.tasks.transaction(candidate['task_id']) as (db, task):
                row = db.execute(select(task_timers).where(task_timers.c.id == candidate['id'])).mappings().one()
                if row['status'] != 'pending':
                    continue
                active = task['status'] == 'open'
                db.execute(update(task_timers).where(task_timers.c.id == row['id']).values(status='fired' if active else 'cancelled'))
                if active:
                    self.tasks.record(db, task, None, 'reminder', row['text'], {'timer_id': row['id']}, wake=bool(row['wake']), event_id=key('timer-fired', row['id']))


class TodoDeadlineSource:
    def __init__(self, tasks):
        self.tasks = tasks

    def poll(self):
        with self.tasks.engine.connect() as db:
            due = db.execute(select(task_todos).where(task_todos.c.status == 'open', task_todos.c.reminded_at.is_(None),
                task_todos.c.due_at <= self.tasks.clock()).limit(100)).mappings().all()
        for candidate in due:
            with self.tasks.transaction(candidate['task_id']) as (db, task):
                row = db.execute(select(task_todos).where(task_todos.c.id == candidate['id'])).mappings().one()
                if row['status'] != 'open' or row['reminded_at'] is not None:
                    continue
                if task['status'] != 'open':
                    continue
                db.execute(update(task_todos).where(task_todos.c.id == row['id']).values(reminded_at=self.tasks.clock()))
                self.tasks.record(db, task, None, 'reminder', '待办已到期：' + row['question'],
                    {'todo_id': row['id'], 'assignee': row['assignee']}, event_id=key('todo-due', row['id']))
