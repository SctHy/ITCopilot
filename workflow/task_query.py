"""Authorized task search, pagination and per-person archives; no worker dependencies."""
from sqlalchemy import select, insert, update, or_
from core.db import service_tasks, task_events, task_archives


class TaskQuery:
    def __init__(self, tasks, annotate):
        self.tasks, self.annotate = tasks, annotate

    def page(self, principal, q='', status='', archived=False, category='all', offset=0, limit=30):
        mark = select(task_archives.c.archived).where(task_archives.c.task_id == service_tasks.c.id, task_archives.c.subject == principal.subject).scalar_subquery()
        query = select(service_tasks).where(self.tasks.access_filter(principal), mark == 1 if archived else or_(mark.is_(None), mark == 0))
        if q:
            has_text = select(task_events.c.id).where(task_events.c.task_id == service_tasks.c.id, task_events.c.text.contains(q, autoescape=True)).exists()
            query = query.where(or_(service_tasks.c.title.contains(q, autoescape=True), has_text))
        if status:
            query = query.where(service_tasks.c.status == status)
        query = query.order_by(service_tasks.c.updated_at.desc(), service_tasks.c.id)
        # Counts and category filtering happen before slicing, so older pending work is reachable.
        with self.tasks.engine.connect() as db:
            rows = [dict(r) for r in db.execute(query).mappings()]
        items = [{k: v for k, v in row.items() if k != 'principal'} | {'requester_name': row['principal']['username']} for row in rows]
        items = self.annotate(items, principal)
        field = {'unread': 'unread', 'answer': 'pending_answers', 'approval': 'pending_approvals'}.get(category)
        if field:
            items = [r for r in items if r[field]]
        return {'items': items[offset:offset + limit], 'total': len(items), 'offset': offset, 'limit': limit, 'has_more': offset + limit < len(items)}

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
