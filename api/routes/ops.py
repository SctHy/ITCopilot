"""运行状态：健康检查、Skill、监控、指标、请求 trace 与离线评测报告。"""
import asyncio
import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from api import state
from api.deps import current_principal, forbid, admin_change, require_admin, require_roles
from core.identity import ROLE_AUDITOR, ROLE_IT_ADMIN, Principal


router = APIRouter()


@router.get("/health")
async def health():
    return {"status": "ok", "lifecycle": state.lifecycle.state if state.lifecycle else "uninitialized", "service": "IT-Copilot", "port": int(os.getenv("IT_COPILOT_PORT", "8001"))}


@router.get("/skills")
async def skills():
    return state.skills.summary() if state.skills else {"count": 0, "skills": []}


@router.post("/skills/reload")
async def reload_skills(principal: Principal = Depends(require_admin)):
    async with admin_change(principal, "skills_reloaded") as change:
        body = state.skills.summary() if state.skills and state.skills.reload() is not None else {"count": 0}
        change.detail = {"count": body.get("count", 0)}
    return body


@router.get("/monitor", dependencies=[Depends(current_principal)])
async def monitor_status():
    """运行状态：Agent/工具实时统计 + 进程内监控（SLO 燃烧率、触发中的告警、处置建议）。"""
    return {
        "agent": state.orchestrator.get_stats() if state.orchestrator else {},
        "tools": state.tool_manager.monitor_snapshot() if state.tool_manager else {},
        "agent_tools": state.orchestrator.tool_executor.snapshot() if state.orchestrator else {},
        "workflow": {"pending_actions": await asyncio.to_thread(state.workflow.pending_work)} if state.workflow else None,
        "channels": await asyncio.to_thread(state.channels.problems) if state.channels else None,
        "monitor": state.monitor.summary() if state.monitor else None,
        "mcp": state.mcp_client.summary() if state.mcp_client else {"enabled": False, "servers": []},
    }


@router.get("/metrics")
async def prometheus_metrics():  # 不能命名为 metrics：会遮蔽上方导入的 monitor.metrics 模块
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def _owns_trace(trace: Optional[Dict[str, Any]], principal: Principal) -> bool:
    scope = (trace or {}).get("observability") or {}
    return scope.get('tenant') == principal.tenant and (principal.can_read_all or scope.get('owner') == principal.subject)


# 静态路径必须在 /trace/{request_id} 之前注册，否则会被当作 request_id 匹配。
@router.get("/trace/tools")
async def trace_tools(limit: int = 20, principal: Principal = Depends(require_roles(ROLE_IT_ADMIN, ROLE_AUDITOR))):
    rows = await state.memory.recent_traces(200) if state.memory else []
    return {"items": [r for r in rows if _owns_trace(r, principal)][:max(1, min(limit, 200))]}


async def _readable_trace(request_id: str, principal: Principal):
    trace = await state.memory.get_trace(request_id) if state.memory else None
    if trace is not None and not _owns_trace(trace, principal):
        raise forbid("只能查看自己的请求 trace")
    return trace


@router.get("/trace/tool/{request_id}")
async def trace_tool(request_id: str, principal: Principal = Depends(current_principal)):
    trace = await _readable_trace(request_id, principal)
    return {"request_id": request_id, "found": trace is not None, "trace": trace or {}}


@router.get("/trace/{request_id}")
async def trace_request(request_id: str, principal: Principal = Depends(current_principal)):
    """Canonical replay endpoint for plan, tools, memory and approval events (owner, admin or auditor only)."""
    trace = await _readable_trace(request_id, principal)
    return {"request_id": request_id, "found": trace is not None, "trace": trace or {}, "schema_version": 1}


@router.post("/ops/retention/run")
async def run_retention(principal: Principal = Depends(require_admin)):
    """立即执行一次保留期清理（后台每小时也会执行）；返回各类删除行数。审批、执行与审计记录不会被清理。"""
    if state.retention is None:
        raise HTTPException(503, "service not ready")
    async with admin_change(principal, "retention_run") as change:
        deleted = await asyncio.to_thread(state.retention.sweep)
        change.detail = deleted
    return {"deleted": deleted, "policy": {
        "channel_days": state.retention.policy.channel_days, "episodic_days": state.retention.policy.episodic_days}}


@router.get("/admin/audit")
async def admin_audit_log(principal: Principal = Depends(require_roles(ROLE_IT_ADMIN, ROLE_AUDITOR)),
                          limit: int = Query(100, ge=1, le=500), before: Optional[int] = Query(None, ge=1),
                          event: Optional[str] = Query(None, max_length=60),
                          outcome: Optional[str] = Query(None, pattern="^(succeeded|failed|interrupted)$")):
    """管理操作审计（知识库增删、策略生效与复核、技能重载、数据清理、令牌吊销）；本租户加全局策略事件，新到旧，按 before 翻页。

    默认列出成功的修改和中断的修改（已开始、没有结果，可能已部分生效，需要核对）；失败的尝试用 outcome=failed 查看。"""
    from core.admin_audit import recent

    if state.workflow is None:
        raise HTTPException(503, "service not ready")
    items = await asyncio.to_thread(recent, state.workflow.engine, principal.tenant, limit=limit, before=before, event=event, outcome=outcome)
    return {"items": items, "next_before": items[-1]["id"] if len(items) == limit else None}
