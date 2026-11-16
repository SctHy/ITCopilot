"""Task acceptance and close policy. All effects use the caller's task transaction.

This policy coordinates service-owned operations; the composition root has no SQL.
Stopping follow-up never cancels approved business actions or revokes access.
"""
from sqlalchemy import select

from core.db import approvals, task_todos
from workflow.states import OPEN, Execution, TaskStatus


def business_ready(db, task):
    pending = db.execute(select(approvals.c.id).where(
        approvals.c.conv_id == 'task:' + task['id'],
        approvals.c.status.in_(OPEN) | approvals.c.execution.in_(
            (Execution.QUEUED, Execution.EXECUTING, Execution.MANUAL_REVIEW)),
    )).first()
    questions = db.execute(select(task_todos.c.id).where(
        task_todos.c.task_id == task['id'], task_todos.c.status == 'open',
    )).first()
    return pending is None and questions is None


class TaskLifecycle:
    def __init__(self, goals, reminders, governance):
        self.goals, self.reminders, self.governance = goals, reminders, governance

    def can_resolve(self, db, task):
        return business_ready(db, task) and self.goals.can_resolve(db, task)

    def changed(self, db, task, status):
        if status != TaskStatus.OPEN:
            self.goals.cancel(db, task)
            self.reminders.cancel_pending(db, task)
            self.governance.close_task(db, task)
