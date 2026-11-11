"""Composition of independent task extensions and approval read models."""
from sqlalchemy import select, update
from core.db import approvals, task_todos, task_timers, task_attempts
from workflow.task_progress import TaskProgressService
from workflow.task_collaboration import TaskCollaborationService
from workflow.task_reminders import TaskReminderService, TodoDeadlineSource
from workflow.task_governance import TaskGovernance
from workflow.task_access import TaskAccess
from workflow.task_query import TaskQuery
from workflow.task_files import TaskFiles
from workflow.task_suggestions import TaskSuggestions
from workflow.task_notifications import TaskNotifications
from workflow.task_checkpoints import TaskPlanStore
from workflow.task_verification import TaskGoalService
from workflow.goal_verifiers import AccessGoalVerifier
from workflow.task_history import TaskHistory


class TaskCapabilities:
    def __init__(self, tasks, approval_view, goal_verifier=None):
        self.tasks, self.approval_view = tasks, approval_view
        self.plan_store = TaskPlanStore(tasks)
        self.goals = TaskGoalService(tasks, goal_verifier or AccessGoalVerifier(), self.business_ready)
        self.progress = TaskProgressService(tasks, self.can_resolve)
        self.history = TaskHistory(tasks, self.progress, approval_view)
        self.collaboration = TaskCollaborationService(tasks)
        self.reminders = TaskReminderService(tasks)
        self.deadlines = TodoDeadlineSource(tasks)
        self.governance = TaskGovernance(tasks)
        tasks.on_status = self.on_status
        self.access = TaskAccess(tasks)
        tasks.access = self.access
        self.query = TaskQuery(tasks, self.annotate)
        self.files = TaskFiles(tasks)
        self.suggestions = TaskSuggestions(tasks, self.progress)
        self.notifications = TaskNotifications(tasks)

    def on_status(self, db, task, status):
        if status != 'open':
            self.goals.cancel(db, task)
            db.execute(update(task_timers).where(task_timers.c.task_id == task['id'], task_timers.c.status == 'pending').values(status='cancelled'))
            db.execute(update(task_attempts).where(task_attempts.c.task_id == task['id'], task_attempts.c.status == 'running')
                       .values(status='interrupted', ended_at=self.tasks.clock(), error='task_closed_usage_unknown'))

    @staticmethod
    def business_ready(db, task):
        pending = db.execute(select(approvals.c.id).where(approvals.c.conv_id == 'task:' + task['id'],
            (approvals.c.status == 'pending') | approvals.c.execution.in_(['queued', 'executing', 'manual_review']))).first()
        questions = db.execute(select(task_todos.c.id).where(task_todos.c.task_id == task['id'], task_todos.c.status == 'open')).first()
        return not pending and not questions

    def can_resolve(self, db, task):
        return self.business_ready(db, task) and self.goals.can_resolve(db, task)

    def workspace(self, task_id, principal):
        return {'verification': self.goals.read(task_id, principal),
                'checkpoints': self.plan_store.list(task_id, principal), 'progress': self.progress.read(task_id, principal),
                'todos': self.collaboration.list(task_id, principal),
                'reminders': self.reminders.list(task_id, principal),
                'files': self.files.list(task_id, principal), 'members': self.access.list(task_id, principal),
                'reviewers': self.access.reviewers(task_id, principal), 'suggestions': self.suggestions.list(task_id, principal),
                'notifications': self.notifications.settings(task_id, principal),
                **self.governance.read(task_id, principal)}

    def inbox(self, principal):
        return self.query.page(principal, limit=100)

    def annotate(self, items, principal):
        items = self.collaboration.inbox(principal, items)
        for item in items:
            decisions = self.approval_view(item).get('approvals', [])
            assigned = self.access.reviewers(item['id'], principal)
            with self.tasks.read(item['id'], principal) as (db, task):
                writable = self.access.writable(db, task, principal)
            item['pending_approvals'] = sum(1 for a in decisions if a['status'] == 'pending'
                and writable and (not assigned or principal.subject in assigned)
                and principal.has_any('approver', 'it_admin') and a['requester'] != principal.subject
                and not any(d['actor'] == principal.subject for d in a['decisions'])
                and not any(str(r['arguments'].get('employee_id')) == str(principal.employee_id) for r in a['action_requests']))
        return items
