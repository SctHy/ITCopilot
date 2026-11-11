"""Transport adapters for independently owned task product capabilities."""
from typing import Literal
from urllib.parse import quote
from fastapi import APIRouter, Depends, Request, Response, HTTPException, Query
from pydantic import BaseModel, Field
from api.deps import current_principal
from api.task_dependencies import service, invoke, Short, Text
from core.identity import Principal
from workflow.task_files import MAX_FILE_SIZE

router = APIRouter(tags=['task product'])


class TodoChange(BaseModel):
    action: Literal['cancel', 'reassign']
    assignee: str | None = Field(default=None, max_length=200)
    reason: Text
    client_request_id: Short


class Member(BaseModel):
    subject: Short
    role: Literal['collaborator', 'observer', 'revoked']


class Reviewers(BaseModel):
    subjects: list[Short] = Field(max_length=20)


class Archive(BaseModel):
    archived: bool


class SuggestionDecision(BaseModel):
    accept: bool


class Subscription(BaseModel):
    channel: Literal['email', 'feishu']
    enabled: bool
    escalate_to: str | None = Field(default=None, max_length=200)


@router.post('/tasks/{task_id}/todos/{todo_id}/change')
async def todo_change(task_id: str, todo_id: str, payload: TodoChange, principal: Principal = Depends(current_principal)):
    await invoke(service().collaboration.change, task_id, todo_id, principal, payload.action, payload.assignee, payload.reason, payload.client_request_id)
    return {'updated': True}


@router.get('/tasks/{task_id}/directory')
async def directory(task_id: str, principal: Principal = Depends(current_principal)):
    return {'items': await invoke(service().access.directory, task_id, principal)}


@router.post('/tasks/{task_id}/members')
async def member(task_id: str, payload: Member, principal: Principal = Depends(current_principal)):
    await invoke(service().access.set_member, task_id, principal, payload.subject, payload.role)
    return {'updated': True}


@router.post('/tasks/{task_id}/reviewers')
async def reviewers(task_id: str, payload: Reviewers, principal: Principal = Depends(current_principal)):
    await invoke(service().access.assign, task_id, principal, list(dict.fromkeys(payload.subjects)))
    return {'updated': True}


@router.post('/tasks/{task_id}/archive')
async def archive(task_id: str, payload: Archive, principal: Principal = Depends(current_principal)):
    await invoke(service().query.archive, task_id, principal, payload.archived)
    return {'updated': True}


@router.get('/tasks/{task_id}/events')
async def events(task_id: str, before: int | None = Query(default=None, ge=0), limit: int = Query(default=50, ge=1, le=100), principal: Principal = Depends(current_principal)):
    return await invoke(service().query.events, task_id, principal, before, limit)


@router.post('/tasks/{task_id}/files')
async def upload(task_id: str, request: Request, name: str = Query(min_length=1, max_length=200), client_request_id: str = Query(min_length=1, max_length=200), principal: Principal = Depends(current_principal)):
    # Authorize before reading a potentially large body, and enforce the limit while streaming.
    await invoke(service().files.list, task_id, principal)
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > MAX_FILE_SIZE:
            raise HTTPException(413, '附件上限为 5 MB')
    return await invoke(service().files.upload, task_id, principal, name, bytes(content), client_request_id)


@router.get('/tasks/{task_id}/files/{file_id}')
async def download(task_id: str, file_id: str, principal: Principal = Depends(current_principal)):
    file = await invoke(service().files.download, task_id, file_id, principal)
    return Response(file['content'], media_type='application/octet-stream', headers={
        'Content-Disposition': "attachment; filename*=UTF-8''" + quote(file['name'], safe=''),
        'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'no-store'})


@router.post('/tasks/{task_id}/suggestions/{suggestion_id}')
async def suggestion(task_id: str, suggestion_id: str, payload: SuggestionDecision, principal: Principal = Depends(current_principal)):
    await invoke(service().suggestions.decide, task_id, suggestion_id, principal, payload.accept)
    return {'updated': True}


@router.post('/tasks/{task_id}/notifications')
async def notifications(task_id: str, payload: Subscription, principal: Principal = Depends(current_principal)):
    await invoke(service().notifications.subscribe, task_id, principal, payload.channel, payload.enabled, payload.escalate_to)
    return {'updated': True}
