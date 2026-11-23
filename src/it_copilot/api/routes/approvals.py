"""审批收件箱、审批决定、人工对账、访问策略与员工业务状态。"""
import asyncio
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from it_copilot.api.container import Container, AppContainer
from it_copilot.api.approval_actions import confirm_approval, release_grants, resolve_manual, submit_decision, withdraw_approval
from it_copilot.api.deps import current_principal, forbid, admin_change, is_platform_admin, require_admin, require_roles, require_workflow
from it_copilot.workflow.store import ApprovalForbidden, ApprovalStateConflict
from it_copilot.api.schemas import ActionResolution, ApprovalConfirmation, ApprovalDecision, ApprovalWithdrawal, ManualResolution, ReviewReason
from it_copilot.infra.env import env_float
from it_copilot.infra.identity import ROLE_APPROVER, ROLE_AUDITOR, ROLE_IT_ADMIN, Principal

from it_copilot.workflow.approval_service import wait_settled

router = APIRouter()


async def _readable_approval(state: AppContainer, approval_id: str, principal: Principal) -> Dict[str, Any]:
    store = require_workflow(state)
    body = await store.run(store.get, approval_id)
    if not body:
        raise HTTPException(404, "approval not found")
    if not store.can_read(body, principal):
        raise forbid("只能查看自己发起的申请")
    return body


@router.get("/approvals")
async def list_approvals(state: Container, status: Optional[str] = Query(None, pattern="^(draft|pending|approved|rejected|expired|superseded|invalidated|withdrawn)$"),
                         principal: Principal = Depends(current_principal)):
    """申请人看到自己的申请；审批人、管理员、审计员看到全部（审批收件箱），但别人尚未确认的草稿只有管理员与审计员可见。"""
    store = require_workflow(state)
    return {"items": await store.run(store.approvals.list, principal, status)}


@router.get("/approvals/{approval_id}")
async def get_approval(state: Container, approval_id: str, principal: Principal = Depends(current_principal)):
    return await _readable_approval(state, approval_id, principal)


@router.post("/approvals/{approval_id}")
async def decide_approval(state: Container, approval_id: str, payload: ApprovalDecision, principal: Principal = Depends(require_roles(ROLE_APPROVER, ROLE_IT_ADMIN))):
    return await submit_decision(state, approval_id, payload, principal)


@router.post("/approvals/{approval_id}/confirm")
async def confirm(state: Container, approval_id: str, payload: ApprovalConfirmation, principal: Principal = Depends(current_principal)):
    """申请人确认 Agent 起草的申请，确认后才会进入审批列表。"""
    return await confirm_approval(state, approval_id, payload.payload_hash, principal)


@router.post("/approvals/{approval_id}/withdraw")
async def withdraw(state: Container, approval_id: str, payload: ApprovalWithdrawal = ApprovalWithdrawal(), principal: Principal = Depends(current_principal)):
    """申请人撤回待审批的申请或取消未确认的草稿；已有审批结果、已过期或被替代的申请不可撤回。"""
    return await withdraw_approval(state, approval_id, payload.reason, principal)


@router.post("/approvals/{approval_id}/release")
async def release(state: Container, approval_id: str, principal: Principal = Depends(current_principal)):
    """提前归还该申请开通的限时权限（本人、权限持有人或 IT 管理员可操作）。"""
    return await release_grants(state, approval_id, principal)


@router.post("/approvals/{approval_id}/reconcile")
async def reconcile_approval(state: Container, approval_id: str, payload: ReviewReason, principal: Principal = Depends(require_admin)):
    """人工重试：把需复核或结果未知的任务重新交给 Worker，先查外部真实状态再决定是否重试。"""
    await _readable_approval(state, approval_id, principal)
    try:
        body = await require_workflow(state).actions.requeue(approval_id, principal, payload.reason)
    except ApprovalStateConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if not body:
        raise HTTPException(404, "approval not found")
    body = await wait_settled(require_workflow(state), approval_id, env_float("IT_COPILOT_APPROVAL_WAIT_S", 5)) or body
    return body


@router.post("/approvals/{approval_id}/resolve")
async def resolve_approval(state: Container, approval_id: str, payload: ManualResolution, principal: Principal = Depends(require_admin)):
    """人工结案：外部系统已手工处理完毕时关闭需复核任务，让同一资源上排队的后续申请继续执行。"""
    await _readable_approval(state, approval_id, principal)
    return await resolve_manual(state, approval_id, payload.note, principal)


@router.post("/approvals/{approval_id}/actions/{action_id}/resolve")
async def resolve_action(state: Container, approval_id: str, action_id: str, payload: ActionResolution,
                         principal: Principal = Depends(require_admin)):
    store = require_workflow(state)
    try:
        body = await store.resolve_action(approval_id, action_id, principal, payload, state.registry)
    except ApprovalForbidden as exc:
        raise HTTPException(403, str(exc)) from exc
    except ApprovalStateConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if body is None:
        raise HTTPException(404, "action not found")
    return body


@router.get("/policy")
async def policy_info(state: Container, principal: Principal = Depends(current_principal)):
    """查询当前生效的访问策略版本；修改策略文件后无需重启服务即可生效。"""
    policy = await asyncio.to_thread(lambda: require_workflow(state).policy)
    return {"version": policy.version, "approval_ttl_hours": policy.approval_ttl_hours}


@router.post("/policy/revalidate")
async def policy_revalidate(state: Container, principal: Principal = Depends(require_admin)):
    """立即按最新策略重新校验处理中的审批和未到期授权（Worker 检测到版本变化时也会自动执行）。
    IT 管理员校验本租户；平台管理员校验所有租户。"""
    store = require_workflow(state)
    scope = None if is_platform_admin(principal) else principal.tenant
    async with admin_change(state, principal, "policy_revalidated", store.policy.version, {"scope": scope or "*"}) as change:
        report = await store.run(store.revalidate, scope)
        change.subject = report["policy_version"]
        change.detail.update({k: len(v) for k, v in report.items() if isinstance(v, list)})
    if state.worker:
        state.worker.kick()
    return report


@router.get("/approvals/{approval_id}/audit")
async def approval_audit(state: Container, approval_id: str, principal: Principal = Depends(current_principal)):
    await _readable_approval(state, approval_id, principal)
    return {"events": await state.workflow.run(state.workflow.audit, approval_id)}


@router.get("/business/employees/{employee_id}")
async def business_employee(state: Container, employee_id: str, principal: Principal = Depends(current_principal)):
    """查询沙箱目录中的员工状态和有效权限记录：本人、审批人、管理员、审计员可查。"""
    if employee_id != principal.employee_id and not principal.has_any(ROLE_APPROVER, ROLE_IT_ADMIN, ROLE_AUDITOR):
        raise forbid("只能查看本人账号状态")
    store = require_workflow(state)
    body = await store.run(store.directory.employee, employee_id, principal.tenant)
    if not body:
        raise HTTPException(404, "employee not found")
    return body
