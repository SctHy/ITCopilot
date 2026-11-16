"""Source-backed discussion summaries; informational only, never task completion evidence."""
import re

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from core.db import task_events
from workflow.tasks import key


class BriefSource(BaseModel):
    event_id: str = Field(min_length=1, max_length=200)
    quote: str = Field(min_length=1, max_length=500)


class BriefItem(BaseModel):
    # Server-expanded user sentences can be longer than the model's short draft.
    text: str = Field(min_length=1, max_length=25000)
    sources: list[BriefSource] = Field(min_length=1, max_length=3)


class TaskBrief(BaseModel):
    reported: list[BriefItem] = Field(default_factory=list, max_length=12)
    answered: list[BriefItem] = Field(default_factory=list, max_length=12)
    hypotheses: list[BriefItem] = Field(default_factory=list, max_length=6)
    questions: list[BriefItem] = Field(default_factory=list, max_length=8)


def source_sentence(text, quote):
    """Expand an exact excerpt to whole sentences, preserving negation and question marks."""
    boundaries = [m.end() for m in re.finditer(r'[。！？!?\n]', text)]
    spans = []
    for match in re.finditer(re.escape(quote), text):
        left = max((i for i in boundaries if i <= match.start()), default=0)
        right = next((i for i in boundaries if i >= match.end()), len(text))
        if spans and left <= spans[-1][1]:
            spans[-1] = (spans[-1][0], max(right, spans[-1][1]))
        else:
            spans.append((left, right))
    return '\n'.join(text[left:right].strip() for left, right in spans)


def preserve_reported(brief, evidence):
    # User statements are extractive. A fluent paraphrase must never turn a question into a fact.
    for category in ('reported', 'answered'):
        unique = {}
        for item in brief.get(category, []):
            item['text'] = '\n'.join(dict.fromkeys(source_sentence(evidence[s['event_id']]['text'], s['quote'])
                                                  for s in item['sources']))
            # Different excerpts of the same source sentence become one displayed item.
            # Keep identical statements from different speakers/events separate.
            identity = (item['text'], tuple(sorted({s['event_id'] for s in item['sources']})))
            unique.setdefault(identity, item)
        brief[category] = list(unique.values())
    return brief


def validate_brief(db, task, event, response, proposal):
    """Reject a whole invalid snapshot instead of erasing previously grounded information."""
    try:
        brief = TaskBrief.model_validate(proposal).model_dump()
    except (ValidationError, TypeError):
        return None
    ids = {source['event_id'] for group in brief.values() for item in group for source in item['sources']}
    rows = db.execute(select(task_events).where(task_events.c.task_id == task['id'],
        task_events.c.seq <= event['seq'], task_events.c.id.in_(ids))).mappings()
    evidence = {row['id']: row for row in rows}
    evidence[key('reply', event['id'])] = {'kind': 'assistant', 'text': response}
    for category, items in brief.items():
        for item in items:
            for source in item['sources']:
                row = evidence.get(source['event_id'])
                if not row or not source['quote'].strip() or source['quote'] not in row['text']:
                    return None
                if category in {'reported', 'answered'} and row['kind'] not in {'requester', 'collaborator', 'approver', 'todo_answer'}:
                    return None
    return preserve_reported(brief, evidence)


class TaskBriefService:
    def __init__(self, tasks):
        self.tasks = tasks

    def read(self, task_id, principal=None, *, before_seq=None):
        with self.tasks.read(task_id, principal) as (db, task):
            query = select(task_events).where(task_events.c.task_id == task_id,
                task_events.c.kind == 'assistant', task_events.c.data['task_brief'].as_string().is_not(None))
            if before_seq is not None:
                query = query.where(task_events.c.seq < before_seq)
            row = db.execute(query.order_by(task_events.c.seq.desc()).limit(1)).mappings().first()
            if not row:
                return None
            brief = TaskBrief.model_validate(row['data']['task_brief']).model_dump()
            ids = {s['event_id'] for group in brief.values() for item in group for s in item['sources']}
            evidence = {e['id']: e for e in db.execute(select(task_events).where(
                task_events.c.task_id == task_id, task_events.c.id.in_(ids))).mappings()}
            # Also protect legacy snapshots on read; immutable historical events stay untouched.
            preserve_reported(brief, evidence)
            for items in brief.values():
                for item in items:
                    for source in item['sources']:
                        original = evidence.get(source['event_id'])
                        if original:
                            source.update(actor_name=original['actor_name'], kind=original['kind'], created_at=original['created_at'])
            original = db.execute(select(task_events).where(task_events.c.task_id == task_id,
                task_events.c.id == key(task_id, 'initial'))).mappings().one()
            origin = {'event_id': original['id'], 'quote': original['text'], 'actor_name': original['actor_name'],
                      'kind': original['kind'], 'created_at': original['created_at']}
            return {**brief, 'origin': origin, 'event_id': row['id'], 'updated_at': row['created_at'],
                    'through_seq': row['data'].get('brief_source_seq', row['seq'])}

    def context(self, task_id, before_seq):
        return self.read(task_id, before_seq=before_seq)
