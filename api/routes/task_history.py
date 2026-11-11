"""User-authorized history search, references and continuation transports."""
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from api.deps import current_principal
from api.task_dependencies import service, invoke, Text, Short, wake_task_worker
from core.identity import Principal

router = APIRouter(tags=['task history'])


class Link(BaseModel):
    target_id: Short
    linked: bool = True


class Continue(BaseModel):
    message: Text
    client_request_id: Short


@router.get('/task-history')
async def search(q: str = Query(default='', max_length=200), limit: int = Query(default=8, ge=1, le=20),
                 exclude_id: str | None = None, principal: Principal = Depends(current_principal)):
    return {'items': await invoke(service().history.search, principal, q, limit, exclude_id)}


@router.get('/task-history/{task_id}')
async def snapshot(task_id: str, principal: Principal = Depends(current_principal)):
    return await invoke(service().history.snapshot, task_id, principal)


@router.post('/task-history/{task_id}/continue')
async def continue_task(task_id: str, payload: Continue, principal: Principal = Depends(current_principal)):
    result = await invoke(service().history.continue_task, task_id, principal, payload.message, payload.client_request_id)
    wake_task_worker()
    return result


@router.get('/tasks/{task_id}/links')
async def links(task_id: str, principal: Principal = Depends(current_principal)):
    return {'items': await invoke(service().history.linked, task_id, principal)}


@router.post('/tasks/{task_id}/links')
async def link(task_id: str, payload: Link, principal: Principal = Depends(current_principal)):
    await invoke(service().history.link, task_id, payload.target_id, principal, payload.linked)
    return {'updated': True}
