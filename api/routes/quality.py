"""Feedback ownership and labeled badcase administration."""
import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from api import state
from api.deps import current_principal, require_roles
from api.routes.tasks import call as task_call
from core.db import task_events, turn_records, read
from core.identity import Principal, ROLE_IT_ADMIN
from core.states import IllegalTransition
from evaluation.badcase_taxonomy import Label
from workflow.badcases import BadcaseConflict, BadcaseNotFound, BadcaseStore

router = APIRouter(tags=["quality"])


class Feedback(BaseModel):
    rating: Literal[-1, 1]
    reason: Literal["wrong_answer", "not_done", "false_claim", "slow", "wrong_language", "unsafe", "other"] = "other"
    comment: str = Field(default="", max_length=500)


def store():
    if state.workflow is None:
        raise HTTPException(503, "service not ready")
    return BadcaseStore(state.workflow.engine)


async def call(method, *args, **kwargs):
    try:
        return await asyncio.to_thread(method, *args, **kwargs)
    except BadcaseNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except (BadcaseConflict, IllegalTransition) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/chat/{request_id}/feedback")
async def chat_feedback(request_id: str, payload: Feedback, principal: Principal = Depends(current_principal)):
    return await call(store().feedback, request_id, principal, payload.rating, payload.reason, payload.comment)


@router.post("/tasks/{task_id}/events/{event_id}/feedback")
async def task_feedback(task_id: str, event_id: str, payload: Feedback, principal: Principal = Depends(current_principal)):
    if state.tasks is None:
        raise HTTPException(503, "service not ready")
    await task_call(state.tasks.get, task_id, principal)
    def lookup():
        with read(state.workflow.engine) as db:
            event = db.execute(select(task_events).where(task_events.c.id == event_id,
                task_events.c.task_id == task_id, task_events.c.kind == "assistant")).mappings().first()
            if event is None:
                raise BadcaseNotFound("Agent回复不存在")
            rid = event["data"]["run_id"]
            turn = db.scalar(select(turn_records.c.request_id).where(turn_records.c.request_id == rid,
                turn_records.c.task_id == task_id, turn_records.c.tenant == principal.tenant))
            if turn is None:
                raise BadcaseNotFound("回合记录不存在")
            return rid
    rid = await call(lookup)
    return await call(store().feedback, rid, principal, payload.rating, payload.reason, payload.comment, shared=True)


@router.get("/admin/badcases")
async def list_badcases(status: str | None = Query(None, pattern="^(new|triaged|labeled|promoted|dismissed)$"),
    root_cause: str | None = None, since: float | None = Query(None, ge=0),
    principal: Principal = Depends(require_roles(ROLE_IT_ADMIN))):
    return {"items": await call(store().list, principal.tenant, status=status, root_cause=root_cause, since=since)}


@router.get("/admin/badcases/{case_id}")
async def get_badcase(case_id: str, principal: Principal = Depends(require_roles(ROLE_IT_ADMIN))):
    return await call(store().get, case_id, principal.tenant)


@router.post("/admin/badcases/{case_id}/label")
async def label_badcase(case_id: str, payload: Label, principal: Principal = Depends(require_roles(ROLE_IT_ADMIN))):
    return await call(store().label, case_id, principal, payload)


@router.post("/admin/badcases/{case_id}/dismiss")
async def dismiss_badcase(case_id: str, principal: Principal = Depends(require_roles(ROLE_IT_ADMIN))):
    return await call(store().transition, case_id, principal, "dismissed")


@router.post("/admin/badcases/{case_id}/promote")
async def promote_badcase(case_id: str, principal: Principal = Depends(require_roles(ROLE_IT_ADMIN))):
    return await call(store().transition, case_id, principal, "promoted")
