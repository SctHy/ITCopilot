"""审批收件箱、审批决定、人工对账、访问策略与员工业务状态。"""
import asyncio
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from api import state
from api.approval_actions import confirm_approval, release_grants, submit_decision, withdraw_approval
from api.deps import current_principal, forbid, admin_change, is_platform_admin, require_admin, require_roles, require_workflow
from api.schemas import ApprovalConfirmation, ApprovalDecision, ApprovalWithdrawal
from core.env import env_float
from core.identity import ROLE_APPROVER, ROLE_AUDITOR, ROLE_IT_ADMIN, Principal

router = APIRouter()


async def _readable_approval(approval_id: str, principal: Principal) -> Dict[str, Any]:
    store = require_workflow()
    body = await asyncio.to_thread(store.get, approval_id)
    if not body:
        raise HTTPException(404, "approval not found")
    if not store.can_read(body, principal):
        raise forbid("只能查看自己发起的申请")
    return body


@router.get("/approvals")
async def list_approvals(status: Optional[str] = Query(None, pattern="^(draft|pending|approved|rejected|expired|superseded|invalidated|withdrawn)$"),
                         principal: Principal = Depends(current_principal)):
    """申请人看到自己的申请；审批人、管理员、审计员看到全部（审批收件箱），但别人尚未确认的草稿只有管理员与审计员可见。"""
    return {"items": await asyncio.to_thread(require_workflow().list, principal, status)}


@router.get("/approvals/{approval_id}")
async def get_approval(approval_id: str, principal: Principal = Depends(current_principal)):
    return await _readable_approval(approval_id, principal)


@router.post("/approvals/{approval_id}")
async def decide_approval(approval_id: str, payload: ApprovalDecision, principal: Principal = Depends(require_roles(ROLE_APPROVER, ROLE_IT_ADMIN))):
    return await submit_decision(approval_id, payload, principal)


@router.post("/approvals/{approval_id}/confirm")
async def confirm(approval_id: str, payload: ApprovalConfirmation, principal: Principal = Depends(current_principal)):
    """申请人确认 Agent 起草的申请，之后才进入审批人队列（ADR 0020）。"""
    return await confirm_approval(approval_id, payload.payload_hash, principal)


@router.post("/approvals/{approval_id}/withdraw")
async def withdraw(approval_id: str, payload: ApprovalWithdrawal = ApprovalWithdrawal(), principal: Principal = Depends(current_principal)):
    """申请人撤回仍待审批的申请或取消未确认的草稿；已有审批结果、已过期或被替代的申请不能撤回。"""
    return await withdraw_approval(approval_id, payload.reason, principal)


@router.post("/approvals/{approval_id}/release")
async def release(approval_id: str, principal: Principal = Depends(current_principal)):
    """提前归还该申请开通、仍在有效期内的限时权限（申请人、权限持有人或 IT 管理员）。"""
    return await release_grants(approval_id, principal)


@router.post("/approvals/{approval_id}/reconcile")
async def reconcile_approval(approval_id: str, principal: Principal = Depends(require_admin)):
    """人工对账：把 manual_review/unknown 的动作重新交给 worker，先回查目标系统再决定是否重做。"""
    await _readable_approval(approval_id, principal)
    body = await asyncio.to_thread(require_workflow().requeue, approval_id, principal)
    if not body:
        raise HTTPException(404, "approval not found")
    if state.worker:
        body = await state.worker.wait_settled(approval_id, env_float("IT_COPILOT_APPROVAL_WAIT_S", 5)) or body
    return body


@router.get("/policy")
async def policy_info(principal: Principal = Depends(current_principal)):
    """调用者所在租户当前生效的访问策略：平台基线加本租户的收紧覆盖（ADR 0025）；改文件后无需重启即生效。"""
    policy = await asyncio.to_thread(require_workflow().policy_for, principal.tenant)
    return {"version": policy.version, "approval_ttl_hours": policy.approval_ttl_hours, "tenant_overlay": policy.tenant is not None}


@router.post("/policy/revalidate")
async def policy_revalidate(principal: Principal = Depends(require_admin)):
    """立即按当前策略复核在途审批与未到期授权（worker 发现版本变化时也会自动执行）。
    IT 管理员复核本租户；平台管理员复核所有租户（ADR 0025）。"""
    store = require_workflow()
    scope = None if is_platform_admin(principal) else principal.tenant
    async with admin_change(principal, "policy_revalidated", store.policy_for(principal.tenant).version, {"scope": scope or "*"}) as change:
        report = await asyncio.to_thread(store.revalidate, scope)
        change.subject = report["policy_version"]
        change.detail.update({k: len(v) for k, v in report.items() if isinstance(v, list)})
    if state.worker:
        state.worker.kick()
    return report


@router.get("/approvals/{approval_id}/audit")
async def approval_audit(approval_id: str, principal: Principal = Depends(current_principal)):
    await _readable_approval(approval_id, principal)
    return {"events": await asyncio.to_thread(state.workflow.audit, approval_id)}


@router.get("/business/employees/{employee_id}")
async def business_employee(employee_id: str, principal: Principal = Depends(current_principal)):
    """沙箱目录中的员工状态与限时授权台账：本人、审批人、管理员、审计员可查。"""
    if employee_id != principal.employee_id and not principal.has_any(ROLE_APPROVER, ROLE_IT_ADMIN, ROLE_AUDITOR):
        raise forbid("只能查看本人账号状态")
    body = await asyncio.to_thread(require_workflow().employee, employee_id, principal.tenant)
    if not body:
        raise HTTPException(404, "employee not found")
    return body
