"""Authorized task search, pagination and per-person archives; no worker dependencies."""
from sqlalchemy import func, insert, or_, select, update
from core import db as schema
from core.db import (
    service_tasks, task_archives, task_events, task_reads, task_todos,
)


def _task_count(table, *conditions):
    return select(func.count()).select_from(table).where(*conditions).correlate(service_tasks).scalar_subquery()


class TaskQuery:
    def __init__(self, tasks, pending_approvals):
        self.tasks = tasks
        self.pending_approvals = pending_approvals

    def page(self, principal, q='', status='', archived=False, category='all', offset=0, limit=30):
        task_id = service_tasks.c.id
        mark = select(1).where(task_archives.c.task_id == task_id, task_archives.c.subject == principal.subject,
                               task_archives.c.archived == 1).exists()
        query = select(service_tasks).where(self.tasks.access_filter(principal), mark if archived else ~mark)
        if q:
            has_text = select(1).where(task_events.c.task_id == task_id, task_events.c.text.contains(q, autoescape=True)).exists()
            query = query.where(or_(service_tasks.c.title.contains(q, autoescape=True), has_text))
        if status:
            query = query.where(service_tasks.c.status == status)

        seen = select(task_reads.c.seq).where(task_reads.c.task_id == task_id,
                    task_reads.c.actor == principal.subject).correlate(service_tasks).scalar_subquery()
        counts = {
            'unread': _task_count(task_events, task_events.c.task_id == task_id,
                                  task_events.c.seq > func.coalesce(seen, 0), task_events.c.actor != principal.subject),
            'pending_answers': _task_count(task_todos, task_todos.c.task_id == task_id,
                                          task_todos.c.assignee == principal.subject, task_todos.c.status == 'open'),
            'pending_approvals': self.pending_approvals(principal, self.tasks.engine.dialect.name),
        }
        field = {'unread': 'unread', 'answer': 'pending_answers', 'approval': 'pending_approvals'}.get(category)
        if field:
            query = query.where(counts[field] > 0)
        page = query.add_columns(*(value.label(name) for name, value in counts.items())).order_by(
            service_tasks.c.updated_at.desc(), task_id).offset(offset).limit(limit)
        with schema.read(self.tasks.engine) as db:
            # Both statements must see the same rows during concurrent membership or status changes.
            if db.dialect.name == 'postgresql':
                db.execution_options(isolation_level='REPEATABLE READ')
            total = db.scalar(select(func.count()).select_from(query.with_only_columns(task_id).subquery()))
            items = [dict(row) for row in db.execute(page).mappings()]
        for item in items:
            item['requester_name'] = item.pop('principal')['username']
        return {'items': items, 'total': total, 'offset': offset, 'limit': limit, 'has_more': offset + limit < total}

    def archive(self, task_id, principal, archived):
        # Archive is a private organization preference, including for observers.
        with self.tasks.transaction(task_id, principal, write=False) as (db, task):
            where = (task_archives.c.task_id == task_id) & (task_archives.c.subject == principal.subject)
            if not db.execute(update(task_archives).where(where).values(archived=int(archived))).rowcount:
                db.execute(insert(task_archives).values(task_id=task_id, subject=principal.subject, archived=int(archived)))

    def events(self, task_id, principal, before=None, limit=50):
        with self.tasks.read(task_id, principal) as (db, task):
            query = select(task_events).where(task_events.c.task_id == task_id)
            if before is not None:
                query = query.where(task_events.c.seq < before)
            rows = db.execute(query.order_by(task_events.c.seq.desc()).limit(limit + 1)).mappings().all()
            return {'items': [self.tasks._public_event(r) for r in reversed(rows[:limit])], 'has_more': len(rows) > limit}
