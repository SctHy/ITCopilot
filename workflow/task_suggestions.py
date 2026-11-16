"""Validated Agent suggestions are staged separately and adopted with a progress CAS."""
from typing import Literal
from pydantic import BaseModel, Field
from sqlalchemy import select, insert, update
from core.db import task_suggestions, task_progress
from workflow.tasks import key, TaskConflict
from workflow.task_brief import TaskBrief, validate_brief


class SuggestedStep(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    status: Literal['todo', 'working', 'done'] = 'todo'
    evidence_ids: list[str] = Field(default_factory=list, max_length=30)


class ProgressSuggestion(BaseModel):
    goal: str = Field(min_length=1, max_length=8000)
    steps: list[SuggestedStep] = Field(min_length=1, max_length=30)
    blocker: str = Field(default='', max_length=4000)
    next_action: str = Field(default='', max_length=4000)
    brief: TaskBrief | None = None


def progress_content(payload):
    content = {k: payload.get(k, [] if k == 'steps' else '') for k in ('goal', 'steps', 'blocker', 'next_action')}
    content['steps'] = [{**step, 'evidence_ids': sorted(set(step.get('evidence_ids', [])))} for step in content['steps']]
    return content


class TaskSuggestions:
    def __init__(self, tasks, progress):
        self.tasks, self.progress = tasks, progress

    def view(self, task):
        return {}

    def commit(self, db, task, event, claim, result):
        proposal = result.get('progress_suggestion')
        if not proposal:
            return {}
        plan = {k: v for k, v in proposal.items() if k != 'brief'}
        payload = ProgressSuggestion.model_validate(plan).model_dump(exclude={'brief'})
        brief = proposal.get('brief')
        extra = {'reply_reviewed': bool(result.get('reply_reviewed'))}
        if brief is not None:
            checked = validate_brief(db, task, event, result['response'], brief)
            extra.update({'task_brief': checked, 'brief_source_seq': event['seq']} if checked is not None else {'brief_unavailable': True})
        version = result.get('progress_version', 0)
        current = db.execute(select(task_progress).where(task_progress.c.task_id == task['id'])).mappings().first()
        current = current or {'version': 0, 'goal': task['title'], 'steps': [], 'blocker': '', 'next_action': ''}
        pending = db.execute(select(task_suggestions).where(task_suggestions.c.task_id == task['id'],
            task_suggestions.c.status == 'pending', task_suggestions.c.base_version == version)
            .order_by(task_suggestions.c.created_at.desc())).mappings().all()
        # Keep the existing decision when review finds no material progress change,
        # even if the proposed prose was rewritten. An empty first plan still needs drafting.
        if proposal.get('progress_changed') is False and version == current['version']:
            if pending:
                return {**extra, 'suggestion_id': pending[0]['id'], 'progress_unchanged': True}
            if current['steps']:
                return {**extra, 'progress_unchanged': True}
        # Identical plans don't create another decision. The brief can still refresh on this reply.
        if version == current['version'] and progress_content(payload) == progress_content(current):
            db.execute(update(task_suggestions).where(task_suggestions.c.task_id == task['id'],
                task_suggestions.c.status == 'pending', task_suggestions.c.base_version <= version).values(status='superseded'))
            return {**extra, 'progress_unchanged': True}
        for old in pending:
            if progress_content(old['payload']) == progress_content(payload):
                return {**extra, 'suggestion_id': old['id']}
        db.execute(update(task_suggestions).where(task_suggestions.c.task_id == task['id'],
            task_suggestions.c.status == 'pending', task_suggestions.c.base_version <= version).values(status='superseded'))
        suggestion_id = key('suggestion', claim['id'])
        db.execute(insert(task_suggestions).values(id=suggestion_id, task_id=task['id'], base_version=version,
            payload=payload, status='pending', created_at=self.tasks.clock()))
        return {**extra, 'suggestion_id': suggestion_id}

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
