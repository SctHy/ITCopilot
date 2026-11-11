"""Authorized task recall and explicit continuation. Links carry no copied content or grants."""
import re
from sqlalchemy import select, insert, delete, or_, func
from core import db as schema
from core.db import service_tasks, task_events, task_progress, task_todos, task_runs, task_links
from workflow.tasks import TaskForbidden, TaskConflict, key
from core.identity import principal_from_record


def search_terms(query):
    """Small deterministic keyword retrieval, including Chinese bigrams; no remote embedding."""
    terms = []
    for word in re.findall(r'[a-z0-9_-]{2,}|[\u4e00-\u9fff]+', query.casefold()):
        if re.fullmatch(r'[\u4e00-\u9fff]+', word) and len(word) > 2:
            terms.extend(word[i:i + 2] for i in range(len(word) - 1))
        else:
            terms.append(word)
    return list(dict.fromkeys(terms))[:24]


class TaskHistory:
    def __init__(self, tasks, progress, approval_view):
        self.tasks, self.progress, self.approval_view = tasks, progress, approval_view

    def runtime_context(self, task_id):
        with self.tasks.read(task_id) as (_, task):
            principal = principal_from_record(task['principal'])
        return self.snapshot(task_id, principal)

    def snapshot(self, task_id, principal):
        # Read-only snapshot: no version bump or write lock (this runs per private chat turn), while
        # TaskStore.read still serializes against a concurrent membership removal.
        with self.tasks.read(task_id, principal) as (db, task):
            row = db.execute(select(task_progress).where(task_progress.c.task_id == task_id)).mappings().first()
            initial = db.execute(select(task_events.c.text).where(task_events.c.id == key(task_id, 'initial'))).scalar() or task['title']
            todo_count = db.execute(select(func.count()).select_from(task_todos).where(task_todos.c.task_id == task_id, task_todos.c.status == 'open')).scalar()
            run_count = db.execute(select(func.count()).select_from(task_runs).where(task_runs.c.task_id == task_id, task_runs.c.status.in_(['queued', 'running', 'held']))).scalar()
            approvals = self.approval_view(task).get('approvals', [])
            writable = self.tasks.access.writable(db, task, principal)
            can_reopen = principal.is_admin or principal.subject == task['requester']
            return {'id': task_id, 'title': task['title'], 'status': task['status'], 'updated_at': task['updated_at'],
                'observed_at': self.tasks.clock(), 'progress_version': row['version'] if row else 0,
                'goal': (row['goal'] if row else initial)[:800], 'resolution': row['resolution'][:800] if row else '',
                'blocker': row['blocker'][:500] if row else '', 'next_action': row['next_action'][:500] if row else '',
                'steps_done': sum(s['status'] == 'done' for s in row['steps']) if row else 0,
                'steps_total': len(row['steps']) if row else 0, 'pending_todos': todo_count, 'active_runs': run_count,
                'approvals': [{k: a[k] for k in ('approval_id', 'status', 'execution')} for a in approvals],
                'can_continue': writable and (task['status'] == 'open' or can_reopen)}

    def search(self, principal, query='', limit=8, exclude_id=None, own_only=False):
        terms = search_terms(query)
        conditions = [self.tasks.access_filter(principal)]
        if exclude_id:
            conditions.append(service_tasks.c.id != exclude_id)
        if own_only:
            conditions.append(service_tasks.c.requester == principal.subject)
        if query.strip():
            if not terms:
                return []
            # Search structured goal/resolution and requester messages. Never return raw transcripts.
            goal_match = select(task_progress.c.task_id).where(task_progress.c.task_id == service_tasks.c.id,
                or_(*[or_(func.lower(task_progress.c.goal).contains(t, autoescape=True),
                          func.lower(task_progress.c.resolution).contains(t, autoescape=True)) for t in terms])).exists()
            request_match = select(task_events.c.id).where(task_events.c.task_id == service_tasks.c.id,
                task_events.c.kind == 'requester', or_(*[func.lower(task_events.c.text).contains(t, autoescape=True) for t in terms])).exists()
            conditions.append(or_(*[func.lower(service_tasks.c.title).contains(t, autoescape=True) for t in terms], goal_match, request_match))
        with schema.read(self.tasks.engine) as db:
            ids = list(db.execute(select(service_tasks.c.id).where(*conditions)
                .order_by(service_tasks.c.updated_at.desc(), service_tasks.c.id).limit(100)).scalars())
        items = []
        for task_id in ids:
            try:
                item = self.snapshot(task_id, principal)  # ACL rechecked after candidate selection
            except TaskForbidden:
                continue
            text = ' '.join(str(item[k]) for k in ('title', 'goal', 'resolution', 'next_action')).casefold()
            item['relevance'] = sum(t in text for t in terms)
            items.append(item)
        items.sort(key=lambda r: (r['relevance'], r['updated_at']), reverse=True)
        return items[:min(20, max(1, limit))]

    def recall_owned(self, principal, query):
        # Only the requester's own tasks enter automatic private-chat memory. An invited
        # participant uses live search; their access can be revoked without leaving profile copies.
        return self.search(principal, query, limit=3, own_only=True)

    def linked(self, task_id, principal):
        with self.tasks.read(task_id, principal) as (db, _):
            ids = list(db.execute(select(task_links.c.target_id).where(task_links.c.task_id == task_id)).scalars())
        items = []
        for target_id in ids:
            try:
                items.append(self.snapshot(target_id, principal))
            except TaskForbidden:
                continue  # do not expose a hidden title, identifier, count, or stale cached summary
        # The destination room can also be revoked while source snapshots are being read.
        with self.tasks.read(task_id, principal):
            return items

    def link(self, task_id, target_id, principal, linked=True):
        if task_id == target_id:
            raise TaskConflict('不能关联事项自身')
        with self.tasks.engine.begin() as db:
            # Canonical lock order avoids A->B / B->A deadlocks.
            for identity in sorted({task_id, target_id} if linked else {task_id}):
                self.tasks._require(db, identity, principal)
                self.tasks._require(db, identity, lock=True)
            task = self.tasks._require(db, task_id, principal)
            self.tasks.access.manage(task, principal)
            if linked:
                self.tasks._require(db, target_id, principal)
            where = (task_links.c.task_id == task_id) & (task_links.c.target_id == target_id)
            exists = db.execute(select(task_links.c.target_id).where(where)).first()
            if linked and not exists:
                db.execute(insert(task_links).values(task_id=task_id, target_id=target_id, created_by=principal.subject, created_at=self.tasks.clock()))
            elif not linked and exists:
                db.execute(delete(task_links).where(where))
            else:
                return
            # Shared events deliberately contain no target title/id; each viewer reads links through ACL.
            self.tasks.record(db, task, principal, 'reference', '更新了关联事项。关联内容仅对具备源事项权限的成员可见。')

    def continue_task(self, task_id, principal, message, client_id):
        with self.tasks.transaction(task_id, principal) as (db, task):
            event_id = key('continue', task_id, principal.scope_key, client_id)
            prior = db.execute(select(task_events).where(task_events.c.id == event_id)).mappings().first()
            if prior:
                if prior['text'] != message:
                    raise TaskConflict('同一请求键不能用于不同跟进消息')
                return {'task_id': task_id, 'event_id': event_id, 'status': task['status']}
            if task['status'] != 'open':
                self.progress.transition(task_id, principal, 'open', '', transaction=(db, task))
            version = db.execute(select(task_progress.c.version).where(task_progress.c.task_id == task_id)).scalar() or 0
            self.tasks.record(db, task, principal, 'continuation', message, {'progress_version': version, 'readonly': True},
                event_id=event_id, wake=True)
            return {'task_id': task_id, 'event_id': event_id, 'status': task['status']}
