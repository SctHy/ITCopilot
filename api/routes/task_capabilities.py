"""HTTP schemas and adapters for task extensions. No business logic in routes."""
from typing import Literal
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from api.deps import current_principal
from core.identity import Principal
from api.task_dependencies import service, invoke, Text, Short

router = APIRouter(tags=['task capabilities'])


class Step(BaseModel):
    title: Short
    status: Literal['todo', 'working', 'done'] = 'todo'
    evidence_ids: list[Short] = Field(default_factory=list, max_length=30)


class Progress(BaseModel):
    version: int = Field(ge=0)
    goal: Text
    steps: list[Step] = Field(max_length=30)
    blocker: str = Field(default='', max_length=4000)
    next_action: str = Field(default='', max_length=4000)


class Transition(BaseModel):
    status: Literal['resolved', 'open']
    resolution: str = Field(default='', max_length=4000)


class Todo(BaseModel):
    question: Text
    assignee: Short
    due_at: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    client_request_id: Short


class Answer(BaseModel):
    answer: Text


class Reminder(BaseModel):
    text: Text
    due_at: float = Field(gt=0, allow_inf_nan=False)
    wake: bool = False
    client_request_id: Short


class Read(BaseModel):
    seq: int = Field(ge=0)


class Budget(BaseModel):
    token_limit: int = Field(ge=1, le=10000000)
    run_limit: int = Field(ge=1, le=10000)


@router.get('/task-inbox')
async def inbox(principal: Principal = Depends(current_principal), q: str = Query(default='', max_length=200),
                status: Literal['', 'open', 'resolved', 'stopped'] = '', archived: bool = False,
                category: Literal['all', 'unread', 'answer', 'approval'] = 'all', offset: int = Query(default=0, ge=0), limit: int = Query(default=30, ge=1, le=100)):
    return await invoke(service().query.page, principal, q, status, archived, category, offset, limit)


@router.get('/tasks/{task_id}/workspace')
async def workspace(task_id: str, principal: Principal = Depends(current_principal)):
    return await invoke(service().workspace, task_id, principal)


@router.post('/tasks/{task_id}/progress')
async def progress(task_id: str, payload: Progress, principal: Principal = Depends(current_principal)):
    return await invoke(service().progress.save, task_id, principal, payload.model_dump())


@router.post('/tasks/{task_id}/lifecycle')
async def transition(task_id: str, payload: Transition, principal: Principal = Depends(current_principal)):
    await invoke(service().progress.transition, task_id, principal, payload.status, payload.resolution)
    return {'status': payload.status}


@router.post('/tasks/{task_id}/todos')
async def todo(task_id: str, payload: Todo, principal: Principal = Depends(current_principal)):
    return await invoke(service().collaboration.create, task_id, principal, payload.model_dump())


@router.post('/tasks/{task_id}/todos/{todo_id}/answer')
async def answer(task_id: str, todo_id: str, payload: Answer, principal: Principal = Depends(current_principal)):
    await invoke(service().collaboration.answer, task_id, todo_id, principal, payload.answer)
    return {'answered': True}


@router.post('/tasks/{task_id}/read')
async def mark_read(task_id: str, payload: Read, principal: Principal = Depends(current_principal)):
    await invoke(service().collaboration.mark_read, task_id, principal, payload.seq)
    return {'read': True}


@router.post('/tasks/{task_id}/reminders')
async def reminder(task_id: str, payload: Reminder, principal: Principal = Depends(current_principal)):
    return await invoke(service().reminders.create, task_id, principal, payload.model_dump())


@router.post('/tasks/{task_id}/reminders/{timer_id}/cancel')
async def cancel(task_id: str, timer_id: str, principal: Principal = Depends(current_principal)):
    await invoke(service().reminders.cancel, task_id, timer_id, principal)
    return {'cancelled': True}


@router.post('/tasks/{task_id}/budget')
async def budget(task_id: str, payload: Budget, principal: Principal = Depends(current_principal)):
    await invoke(service().governance.configure, task_id, principal, payload.token_limit, payload.run_limit)
    return {'updated': True}
