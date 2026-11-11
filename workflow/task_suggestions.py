"""Validated Agent suggestions are staged separately and adopted with a progress CAS."""
from typing import Literal
from pydantic import BaseModel, Field
from sqlalchemy import select, insert, update
from core.db import task_suggestions
from workflow.tasks import key, TaskConflict


class SuggestedStep(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    status: Literal['todo', 'working', 'done'] = 'todo'
    evidence_ids: list[str] = Field(default_factory=list, max_length=30)


class ProgressSuggestion(BaseModel):
    goal: str = Field(min_length=1, max_length=8000)
    steps: list[SuggestedStep] = Field(min_length=1, max_length=30)
    blocker: str = Field(default='', max_length=4000)
    next_action: str = Field(default='', max_length=4000)


class TaskSuggestions:
    def __init__(self, tasks, progress):
        self.tasks, self.progress = tasks, progress

    def view(self, task):
        return {}

    def commit(self, db, task, event, claim, result):
        proposal = result.get('progress_suggestion')
        if not proposal:
            return {}
        payload = ProgressSuggestion.model_validate(proposal).model_dump()
        suggestion_id = key('suggestion', claim['id'])
        db.execute(insert(task_suggestions).values(id=suggestion_id, task_id=task['id'], base_version=result.get('progress_version', 0),
            payload=payload, status='pending', created_at=self.tasks.clock()))
        return {'suggestion_id': suggestion_id}

    def list(self, task_id, principal):
        with self.tasks.read(task_id, principal) as (db, task):
            return [dict(r) for r in db.execute(select(task_suggestions).where(task_suggestions.c.task_id == task_id)
                .order_by(task_suggestions.c.created_at.desc()).limit(20)).mappings()]

    def decide(self, task_id, suggestion_id, principal, accept):
        with self.tasks.transaction(task_id, principal) as (db, task):
            row = db.execute(select(task_suggestions).where(task_suggestions.c.id == suggestion_id, task_suggestions.c.task_id == task_id)).mappings().first()
            if not row:
                raise TaskConflict('建议不存在')
            wanted = 'accepted' if accept else 'dismissed'
            if row['status'] == wanted:
                return
            if row['status'] != 'pending':
                raise TaskConflict('建议已被处理')
            if accept:
                self.progress.save(task_id, principal, {**row['payload'], 'version': row['base_version']}, transaction=(db, task))
            db.execute(update(task_suggestions).where(task_suggestions.c.id == suggestion_id).values(status=wanted))
            self.tasks.record(db, task, principal, 'suggestion', '采纳了 Agent 进度建议。' if accept else '忽略了 Agent 进度建议。', {'suggestion_id': suggestion_id})
