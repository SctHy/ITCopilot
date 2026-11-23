"""HTTP composition and error translation for the shared approval service."""

from fastapi import HTTPException

from servicedesk_agent.api.container import AppContainer
from servicedesk_agent.api.deps import require_workflow
from servicedesk_agent.infra.env import env_float
from servicedesk_agent.infra.monitor import metrics
from servicedesk_agent.workflow.approval_service import ApprovalService, wait_revoked
from servicedesk_agent.workflow.store import ApprovalConflict, ApprovalForbidden, ApprovalStateConflict


async def submit_decision(state: AppContainer, approval_id, payload, principal):
    service = ApprovalService(require_workflow(state), wait_s=env_float("SERVICEDESK_AGENT_APPROVAL_WAIT_S", 5))
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
        body = await store.run(store.confirm, approval_id, principal, payload_hash)
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
        before = await store.run(store.get, approval_id)
        body = await store.run(store.withdraw, approval_id, principal, reason)
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
    """提前归还该申请开通的限时权限：把到期回收任务改为立即执行，执行前仍会检查外部状态是否一致。"""
    store = require_workflow(state)
    try:
        body = await store.run(store.release, approval_id, principal)
    except ApprovalForbidden as exc:
        raise HTTPException(403, str(exc)) from exc
    except ApprovalStateConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if not body:
        raise HTTPException(404, "approval not found")
    released = body["released"]
    if released:
        metrics.APPROVALS.labels(event="grant_released").inc(len(released))
        body = {**(await wait_revoked(store, approval_id, env_float("SERVICEDESK_AGENT_APPROVAL_WAIT_S", 5)) or body), "released": released}
    return body


async def resolve_manual(state: AppContainer, approval_id, note, principal):
    """人工结案需复核的任务；取消后的回收不再计入有效记录，同一资源的后续任务可以继续执行。"""
    store = require_workflow(state)
    try:
        body = await store.run(store.resolve_manual, approval_id, principal, note)
    except ApprovalForbidden as exc:
        raise HTTPException(403, str(exc)) from exc
    except ApprovalStateConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if not body:
        raise HTTPException(404, "approval not found")
    if state.worker:
        state.worker.kick()
    return body
