"""Requester, Agent and reviewers share one durable service room."""
import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, StringConstraints

from api import state
from api.deps import current_principal, enforce_rate_limit
from api.schemas import ApprovalDecision
from api.approval_actions import submit_decision
from core.identity import Principal
from workflow.tasks import TaskConflict, TaskForbidden
from core.rate_limit import AGENT_TURN

router = APIRouter(prefix="/tasks", tags=["shared tasks"])
Message = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)]


class NewTask(BaseModel):
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    message: Message
    client_request_id: str = Field(min_length=1, max_length=200)


class TaskMessage(BaseModel):
    message: Message
    client_request_id: str = Field(min_length=1, max_length=200)
    ask_agent: bool = True


def store():
    if state.tasks is None:
        raise HTTPException(503, "shared task service not ready")
    return state.tasks


async def call(method, *args):
    try:
        return await asyncio.to_thread(method, *args)
    except TaskForbidden as exc:
        raise HTTPException(403, str(exc)) from exc
    except TaskConflict as exc:
        raise HTTPException(409, str(exc)) from exc


def wake():
    if state.task_worker:
        state.task_worker.kick()


@router.get("")
async def list_tasks(principal: Principal = Depends(current_principal)):
    return {"items": await call(store().list, principal)}


@router.post("", status_code=201)
async def create_task(payload: NewTask, principal: Principal = Depends(current_principal)):
    body = await call(store().create, principal, payload.title, payload.message, payload.client_request_id)
    wake()
    return body


@router.get("/{task_id}")
async def get_task(task_id: str, principal: Principal = Depends(current_principal), event_limit: int | None = Query(default=None, ge=1, le=100)):
    return await call(store().get, task_id, principal, event_limit)


@router.post("/{task_id}/messages", status_code=202)
async def post_message(task_id: str, payload: TaskMessage, principal: Principal = Depends(current_principal)):
    if payload.ask_agent:  # plain discussion does not start model work
        await enforce_rate_limit(principal, AGENT_TURN)
    body = await call(store().post, task_id, principal, payload.message, payload.client_request_id, payload.ask_agent)
    wake()
    return body


@router.post("/{task_id}/stop")
async def stop_task(task_id: str, principal: Principal = Depends(current_principal)):
    return await call(store().stop, task_id, principal)


@router.post("/{task_id}/runs/{run_id}/retry", status_code=202)
async def retry_run(task_id: str, run_id: str, principal: Principal = Depends(current_principal)):
    await call(store().retry, task_id, run_id, principal)
    wake()
    return {"queued": True}


@router.post("/{task_id}/approvals/{approval_id}")
async def decide_in_room(task_id: str, approval_id: str, payload: ApprovalDecision, principal: Principal = Depends(current_principal)):
    room = await call(store().get, task_id, principal)
    if not any(a["approval_id"] == approval_id for a in room["approvals"]):
        raise HTTPException(404, "审批不属于该事项")
    # The workflow still enforces authenticated role, separation of duties and hash.
    if not principal.has_any("approver", "it_admin"):
        raise HTTPException(403, "当前身份没有审批权限")
    return await submit_decision(approval_id, payload, principal)
