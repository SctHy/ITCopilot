"""Questions, replies and read cursors. Does not decide approvals or run the Agent."""
from sqlalchemy import insert, select, update, func
from core.db import task_todos, task_reads, task_events
from workflow.tasks import TaskConflict, TaskForbidden, key


class TaskCollaborationService:
    def __init__(self, tasks):
        self.tasks = tasks

    def list(self, task_id, principal):
        with self.tasks.read(task_id, principal) as (db, task):
            return [dict(r) for r in db.execute(select(task_todos).where(task_todos.c.task_id == task_id).order_by(task_todos.c.created_at)).mappings()]

    def create(self, task_id, principal, payload):
        with self.tasks.transaction(task_id, principal) as (db, task):
            if task['status'] != 'open':
                raise TaskConflict('事项当前不能添加待办')
            self.tasks.access.require_assignee(db, task, payload['assignee'], principal)
            todo_id = key(task_id, principal.subject, 'todo', payload['client_request_id'])
            old = db.execute(select(task_todos).where(task_todos.c.id == todo_id)).mappings().first()
            if old:
                if any(old[k] != payload[k] for k in ('assignee', 'question', 'due_at')):
                    raise TaskConflict('同一请求键不能用于不同待办')
                return dict(old)
            row = dict(id=todo_id, task_id=task_id, creator=principal.subject, assignee=payload['assignee'],
                       question=payload['question'], due_at=payload['due_at'], answer='', status='open',
                       created_at=self.tasks.clock(), reminded_at=None)
            db.execute(insert(task_todos).values(**row))
            self.tasks.record(db, task, principal, 'todo', '请求补充：' + row['question'], {'todo_id': todo_id, 'assignee': row['assignee']})
            return row

    def answer(self, task_id, todo_id, principal, answer):
        with self.tasks.transaction(task_id, principal) as (db, task):
            row = db.execute(select(task_todos).where(task_todos.c.id == todo_id, task_todos.c.task_id == task_id)).mappings().first()
            if not row or row['assignee'] != principal.subject:
                raise TaskForbidden('只有指定回答人可以答复此待办')
            if row['status'] != 'open':
                if row['answer'] == answer:
                    return
                raise TaskConflict('该待办已答复')
            if task['status'] != 'open':
                raise TaskConflict('请先重新打开事项')
            db.execute(update(task_todos).where(task_todos.c.id == todo_id).values(status='answered', answer=answer))
            # A reply wakes a read-only follow-up; it is never a new write request.
            self.tasks.record(db, task, principal, 'todo_answer', answer, {'todo_id': todo_id}, wake=True)

    def change(self, task_id, todo_id, principal, action, assignee, reason, client_id):
        with self.tasks.transaction(task_id, principal) as (db, task):
            row = db.execute(select(task_todos).where(task_todos.c.id == todo_id, task_todos.c.task_id == task_id)).mappings().first()
            if not row or (row['creator'] != principal.subject and not principal.is_admin):
                raise TaskForbidden('只有提问人或管理员可以取消或改派')
            event_id = key(task_id, principal.subject, 'todo-change', client_id)
            payload = dict(todo_id=todo_id, action=action, assignee=assignee, reason=reason)
            prior = db.execute(select(task_events.c.data).where(task_events.c.id == event_id)).scalar()
            if prior:
                if any(prior[k] != payload[k] for k in payload):
                    raise TaskConflict('同一请求键不能用于不同操作')
                return
            if row['status'] != 'open':
                raise TaskConflict('只支持处理未完成待办')
            if action == 'reassign':
                self.tasks.access.require_assignee(db, task, assignee, principal)
                db.execute(update(task_todos).where(task_todos.c.id == todo_id).values(assignee=assignee, reminded_at=None))
            else:
                db.execute(update(task_todos).where(task_todos.c.id == todo_id).values(status='cancelled'))
            self.tasks.record(db, task, principal, 'todo_changed', ('改派' if action == 'reassign' else '取消') + '待办：' + reason,
                {**payload, 'previous_assignee': row['assignee']}, event_id=event_id)

    def mark_read(self, task_id, principal, seq):
        with self.tasks.transaction(task_id, principal, write=False) as (db, task):
            latest = db.execute(select(func.max(task_events.c.seq)).where(task_events.c.task_id == task_id)).scalar() or 0
            if seq > latest:
                raise TaskConflict('不能标记尚未出现的消息')
            where = (task_reads.c.task_id == task_id) & (task_reads.c.actor == principal.subject)
            old = db.execute(select(task_reads).where(where)).mappings().first()
            if old:
                db.execute(update(task_reads).where(where).values(seq=max(seq, old['seq'])))
            else:
                db.execute(insert(task_reads).values(task_id=task_id, actor=principal.subject, seq=seq))

    def inbox(self, principal, items=None):
        items = self.tasks.list(principal) if items is None else items
        with self.tasks.engine.connect() as db:
            for item in items:
                seen = db.execute(select(task_reads.c.seq).where(task_reads.c.task_id == item['id'], task_reads.c.actor == principal.subject)).scalar() or 0
                item['unread'] = db.execute(select(func.count()).select_from(task_events).where(task_events.c.task_id == item['id'], task_events.c.seq > seen, task_events.c.actor != principal.subject)).scalar()
                item['pending_answers'] = db.execute(select(func.count()).select_from(task_todos).where(task_todos.c.task_id == item['id'], task_todos.c.assignee == principal.subject, task_todos.c.status == 'open')).scalar()
        return items
