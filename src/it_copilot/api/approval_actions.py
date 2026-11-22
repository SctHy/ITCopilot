"""HTTP composition and error translation for the shared approval service."""
import asyncio

from fastapi import HTTPException

from it_copilot.api.container import AppContainer
from it_copilot.api.deps import require_workflow
from it_copilot.infra.env import env_float
from it_copilot.infra.monitor import metrics
from it_copilot.workflow.approval_service import ApprovalService
from it_copilot.workflow.store import ApprovalConflict, ApprovalForbidden, ApprovalStateConflict


async def submit_decision(state: AppContainer, approval_id, payload, principal):
    service = ApprovalService(require_workflow(state), state.worker, wait_s=env_float("IT_COPILOT_APPROVAL_WAIT_S", 5))
    try:
        body = await service.decide(approval_id, payload.decision, payload.payload_hash, principal)
    except ApprovalForbidden as exc:
        metrics.AUTH_FAILURES.labels(reason="separation_of_duties").inc()
        raise HTTPException(403, str(exc)) from exc
    except ApprovalStateConflict as exc:  # e.g. a draft the requester has not confirmed yet
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return body


async def confirm_approval(state: AppContainer, approval_id, payload_hash, principal):
    """申请人确认草稿：进入审批人队列。与审批决定共用 CAS 与审计；再次确认幂等。"""
    store = require_workflow(state)
    try:
        body = await asyncio.to_thread(store.confirm, approval_id, principal, payload_hash)
    except ApprovalForbidden as exc:
        raise HTTPException(403, str(exc)) from exc
    except ApprovalConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ApprovalStateConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if not body:
        raise HTTPException(404, "approval not found")
    if body["status"] == "pending":
        metrics.APPROVALS.labels(event="confirmed").inc()
    return body


async def withdraw_approval(state: AppContainer, approval_id, reason, principal):
    """申请人撤回仍待审批的申请；与审批决定共用 CAS、审计与通知事务。"""
    store = require_workflow(state)
    try:
        before = await asyncio.to_thread(store.get, approval_id)
        body = await asyncio.to_thread(store.withdraw, approval_id, principal, reason)
    except ApprovalForbidden as exc:
        raise HTTPException(403, str(exc)) from exc
    except ApprovalStateConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if not body:
        raise HTTPException(404, "approval not found")
    if before and before["status"] in {"draft", "pending"} and body["status"] != before["status"]:
        metrics.APPROVALS.labels(event=body["status"]).inc()
    return body


async def release_grants(state: AppContainer, approval_id, principal):
    """提前归还该申请开通的限时权限：到期回收动作改为立即执行，仍经过漂移检查。"""
    store = require_workflow(state)
    try:
        body = await asyncio.to_thread(store.release, approval_id, principal)
    except ApprovalForbidden as exc:
        raise HTTPException(403, str(exc)) from exc
    except ApprovalStateConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if not body:
        raise HTTPException(404, "approval not found")
    released = body["released"]
    if released:
        metrics.APPROVALS.labels(event="grant_released").inc(len(released))
        if state.worker:
            body = {**(await state.worker.wait_revoked(approval_id, env_float("IT_COPILOT_APPROVAL_WAIT_S", 5)) or body), "released": released}
    return body


async def resolve_manual(state: AppContainer, approval_id, note, principal):
    """人工结案需复核的动作；取消的回收不再计入台账，后续同资源动作可以继续执行。"""
    store = require_workflow(state)
    try:
        body = await asyncio.to_thread(store.resolve_manual, approval_id, principal, note)
    except ApprovalForbidden as exc:
        raise HTTPException(403, str(exc)) from exc
    except ApprovalStateConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if not body:
        raise HTTPException(404, "approval not found")
    if state.worker:
        state.worker.kick()
    return body
