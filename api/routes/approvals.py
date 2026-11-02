"""审批收件箱、审批决定、人工对账、访问策略与员工业务状态。"""
import asyncio
import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from api import state
from api.deps import current_principal, forbid, require_admin, require_roles, require_workflow
from api.schemas import ApprovalDecision
from core.identity import ROLE_APPROVER, ROLE_AUDITOR, ROLE_IT_ADMIN, Principal
from monitor import metrics
from workflow.store import ApprovalForbidden

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
async def list_approvals(status: Optional[str] = Query(None, pattern="^(pending|approved|rejected|expired|superseded|invalidated)$"),
                         principal: Principal = Depends(current_principal)):
    """申请人看到自己的申请；审批人、管理员、审计员看到全部（审批收件箱）。"""
    return {"items": await asyncio.to_thread(require_workflow().list, principal, status)}


@router.get("/approvals/{approval_id}")
async def get_approval(approval_id: str, principal: Principal = Depends(current_principal)):
    return await _readable_approval(approval_id, principal)


@router.post("/approvals/{approval_id}")
async def decide_approval(approval_id: str, payload: ApprovalDecision, principal: Principal = Depends(require_roles(ROLE_APPROVER, ROLE_IT_ADMIN))):
    store = require_workflow()
    before = await asyncio.to_thread(store.get, approval_id)
    try:
        body = await asyncio.to_thread(store.decide, approval_id, payload.decision, principal, payload.payload_hash)
    except ApprovalForbidden as exc:
        metrics.AUTH_FAILURES.labels(reason="separation_of_duties").inc()
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not body:
        raise HTTPException(404, "approval not found")
    if before and before["status"] == "pending" and body["status"] != "pending":
        # 只统计真正发生的状态迁移；重复提交的幂等返回不计数。
        metrics.APPROVALS.labels(event=body["status"]).inc()
    if body["status"] == "approved" and state.worker:
        # 执行由持久 worker 完成；这里只短暂等待，让快速路径直接返回结果，慢的外部系统返回 queued/executing。
        body = await state.worker.wait_settled(approval_id, float(os.getenv("IT_COPILOT_APPROVAL_WAIT_S", "5"))) or body
    return body


@router.post("/approvals/{approval_id}/reconcile")
async def reconcile_approval(approval_id: str, principal: Principal = Depends(require_admin)):
    """人工对账：把 manual_review/unknown 的动作重新交给 worker，先回查目标系统再决定是否重做。"""
    body = await asyncio.to_thread(require_workflow().requeue, approval_id, principal)
    if not body:
        raise HTTPException(404, "approval not found")
    if state.worker:
        body = await state.worker.wait_settled(approval_id, float(os.getenv("IT_COPILOT_APPROVAL_WAIT_S", "5"))) or body
    return body


@router.get("/policy", dependencies=[Depends(current_principal)])
async def policy_info():
    """当前生效的访问策略版本；改 config/access_policy.yaml 并升版本号后无需重启即生效。"""
    policy = await asyncio.to_thread(lambda: require_workflow().policy)
    return {"version": policy.version, "approval_ttl_hours": policy.approval_ttl_hours}


@router.post("/policy/revalidate", dependencies=[Depends(require_admin)])
async def policy_revalidate():
    """立即按当前策略复核在途审批与未到期授权（worker 发现版本变化时也会自动执行）。"""
    report = await asyncio.to_thread(require_workflow().revalidate)
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
    body = await asyncio.to_thread(require_workflow().employee, employee_id)
    if not body:
        raise HTTPException(404, "employee not found")
    return body
