"""审批收件箱、审批决定、人工对账、访问策略与员工业务状态。"""
import asyncio
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from it_copilot.api.container import Container, AppContainer
from it_copilot.api.approval_actions import confirm_approval, release_grants, resolve_manual, submit_decision, withdraw_approval
from it_copilot.api.deps import current_principal, forbid, admin_change, is_platform_admin, require_admin, require_roles, require_workflow
from it_copilot.api.schemas import ApprovalConfirmation, ApprovalDecision, ApprovalWithdrawal, ManualResolution
from it_copilot.infra.env import env_float
from it_copilot.infra.identity import ROLE_APPROVER, ROLE_AUDITOR, ROLE_IT_ADMIN, Principal

router = APIRouter()


async def _readable_approval(state: AppContainer, approval_id: str, principal: Principal) -> Dict[str, Any]:
    store = require_workflow(state)
    body = await asyncio.to_thread(store.get, approval_id)
    if not body:
        raise HTTPException(404, "approval not found")
    if not store.can_read(body, principal):
        raise forbid("只能查看自己发起的申请")
    return body


@router.get("/approvals")
async def list_approvals(state: Container, status: Optional[str] = Query(None, pattern="^(draft|pending|approved|rejected|expired|superseded|invalidated|withdrawn)$"),
                         principal: Principal = Depends(current_principal)):
    """申请人看到自己的申请；审批人、管理员、审计员看到全部（审批收件箱），但别人尚未确认的草稿只有管理员与审计员可见。"""
    return {"items": await asyncio.to_thread(require_workflow(state).list, principal, status)}


@router.get("/approvals/{approval_id}")
async def get_approval(state: Container, approval_id: str, principal: Principal = Depends(current_principal)):
    return await _readable_approval(state, approval_id, principal)


@router.post("/approvals/{approval_id}")
async def decide_approval(state: Container, approval_id: str, payload: ApprovalDecision, principal: Principal = Depends(require_roles(ROLE_APPROVER, ROLE_IT_ADMIN))):
    return await submit_decision(state, approval_id, payload, principal)


@router.post("/approvals/{approval_id}/confirm")
async def confirm(state: Container, approval_id: str, payload: ApprovalConfirmation, principal: Principal = Depends(current_principal)):
    """申请人确认 Agent 起草的申请，之后才进入审批人队列（ADR 0020）。"""
    return await confirm_approval(state, approval_id, payload.payload_hash, principal)


@router.post("/approvals/{approval_id}/withdraw")
async def withdraw(state: Container, approval_id: str, payload: ApprovalWithdrawal = ApprovalWithdrawal(), principal: Principal = Depends(current_principal)):
    """申请人撤回仍待审批的申请或取消未确认的草稿；已有审批结果、已过期或被替代的申请不能撤回。"""
    return await withdraw_approval(state, approval_id, payload.reason, principal)


@router.post("/approvals/{approval_id}/release")
async def release(state: Container, approval_id: str, principal: Principal = Depends(current_principal)):
    """提前归还该申请开通、仍在有效期内的限时权限（申请人、权限持有人或 IT 管理员）。"""
    return await release_grants(state, approval_id, principal)


@router.post("/approvals/{approval_id}/reconcile")
async def reconcile_approval(state: Container, approval_id: str, principal: Principal = Depends(require_admin)):
    """人工对账：把 manual_review/unknown 的动作重新交给 worker，先回查目标系统再决定是否重做。"""
    await _readable_approval(state, approval_id, principal)
    body = await asyncio.to_thread(require_workflow(state).requeue, approval_id, principal)
    if not body:
        raise HTTPException(404, "approval not found")
    if state.worker:
        body = await state.worker.wait_settled(approval_id, env_float("IT_COPILOT_APPROVAL_WAIT_S", 5)) or body
    return body


@router.post("/approvals/{approval_id}/resolve")
async def resolve_approval(state: Container, approval_id: str, payload: ManualResolution, principal: Principal = Depends(require_admin)):
    """人工结案：目标系统已人工处理时结束需复核的动作，释放该资源上排队的后续申请。"""
    await _readable_approval(state, approval_id, principal)
    return await resolve_manual(state, approval_id, payload.note, principal)


@router.get("/policy")
async def policy_info(state: Container, principal: Principal = Depends(current_principal)):
    """当前生效的访问策略版本；改文件后无需重启即生效。"""
    policy = await asyncio.to_thread(lambda: require_workflow(state).policy)
    return {"version": policy.version, "approval_ttl_hours": policy.approval_ttl_hours}


@router.post("/policy/revalidate")
async def policy_revalidate(state: Container, principal: Principal = Depends(require_admin)):
    """立即按当前策略复核在途审批与未到期授权（worker 发现版本变化时也会自动执行）。
    IT 管理员复核本租户；平台管理员复核所有租户。"""
    store = require_workflow(state)
    scope = None if is_platform_admin(principal) else principal.tenant
    async with admin_change(state, principal, "policy_revalidated", store.policy.version, {"scope": scope or "*"}) as change:
        report = await asyncio.to_thread(store.revalidate, scope)
        change.subject = report["policy_version"]
        change.detail.update({k: len(v) for k, v in report.items() if isinstance(v, list)})
    if state.worker:
        state.worker.kick()
    return report


@router.get("/approvals/{approval_id}/audit")
async def approval_audit(state: Container, approval_id: str, principal: Principal = Depends(current_principal)):
    await _readable_approval(state, approval_id, principal)
    return {"events": await asyncio.to_thread(state.workflow.audit, approval_id)}


@router.get("/business/employees/{employee_id}")
async def business_employee(state: Container, employee_id: str, principal: Principal = Depends(current_principal)):
    """沙箱目录中的员工状态与限时授权台账：本人、审批人、管理员、审计员可查。"""
    if employee_id != principal.employee_id and not principal.has_any(ROLE_APPROVER, ROLE_IT_ADMIN, ROLE_AUDITOR):
        raise forbid("只能查看本人账号状态")
    body = await asyncio.to_thread(require_workflow(state).employee, employee_id, principal.tenant)
    if not body:
        raise HTTPException(404, "employee not found")
    return body
