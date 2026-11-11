"""Shared task execution controls; no model-controlled goal or acceptance mutations."""
from typing import Literal
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from api.deps import current_principal
from api.task_dependencies import Short, service, invoke
from core.identity import Principal

router = APIRouter(tags=['task execution'])


class Check(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['access_level']
    system: Short
    role: Short


class Goal(BaseModel):
    version: int = Field(ge=0)
    checks: list[Check] = Field(max_length=10)
    replan_limit: int = Field(default=2, ge=0, le=2)


class Verify(BaseModel):
    client_request_id: Short


@router.post('/tasks/{task_id}/goal')
async def configure(task_id: str, payload: Goal, principal: Principal = Depends(current_principal)):
    await invoke(service().goals.configure, task_id, principal, payload.version,
                 [c.model_dump() for c in payload.checks], payload.replan_limit)
    return {'updated': True}


@router.post('/tasks/{task_id}/verify')
async def verify(task_id: str, payload: Verify, principal: Principal = Depends(current_principal)):
    job_id = await invoke(service().goals.request, task_id, principal, payload.client_request_id)
    return {'verification_id': job_id}
