"""Composition of independent task extensions and approval read models."""
from workflow.task_lifecycle import TaskLifecycle, business_ready
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
from workflow.task_brief import TaskBriefService


class TaskCapabilities:
    def __init__(self, tasks, approval_view, goal_verifier=None):
        self.tasks, self.approval_view = tasks, approval_view
        self.plan_store = TaskPlanStore(tasks)
        self.goals = TaskGoalService(tasks, goal_verifier or AccessGoalVerifier(), business_ready)
        self.collaboration = TaskCollaborationService(tasks)
        self.reminders = TaskReminderService(tasks)
        self.deadlines = TodoDeadlineSource(tasks)
        self.governance = TaskGovernance(tasks)
        self.access = TaskAccess(tasks)
        self.lifecycle = TaskLifecycle(self.goals, self.reminders, self.governance)
        tasks.configure_extensions(access=self.access, governance=self.governance, on_status=self.lifecycle.changed)
        self.progress = TaskProgressService(tasks, self.lifecycle.can_resolve)
        self.history = TaskHistory(tasks, self.progress, approval_view)
        self.query = TaskQuery(tasks, self.annotate)
        self.files = TaskFiles(tasks)
        self.suggestions = TaskSuggestions(tasks, self.progress)
        self.brief = TaskBriefService(tasks)
        self.notifications = TaskNotifications(tasks)

    def workspace(self, task_id, principal):
        return {'brief': self.brief.read(task_id, principal), 'verification': self.goals.read(task_id, principal),
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
