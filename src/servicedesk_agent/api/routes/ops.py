"""运行状态：健康检查、Skill、监控、指标、请求 trace 与离线评测报告。"""
import os
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from servicedesk_agent.api.container import Container, AppContainer
from servicedesk_agent.api.deps import current_principal, forbid, admin_change, require_platform_admin, require_roles
from servicedesk_agent.infra.identity import ROLE_AUDITOR, ROLE_IT_ADMIN, Principal


router = APIRouter()


class ModelDefault(BaseModel):
    route: Literal["agent_read", "agent_write"]
    model: str | None = None
    thinking: Literal["disabled", "low", "high", "max"] | None = None
    reset: bool = False


@router.get("/models")
async def available_models(state: Container, principal: Principal = Depends(current_principal)):
    if not state.orchestrator:
        raise HTTPException(503, "service not ready")
    routing = state.orchestrator.router
    routing.reload()
    defaults = {}
    for route in ("agent_read", "agent_write"):
        choices, level = routing.preferences(route, principal.tenant)
        defaults[route] = {"model": choices[0], "thinking": level}
    policy = routing.config.tenants.get(principal.tenant, routing.config.tenants["default"])
    models = [{"id": name, "name": spec.name, "api": spec.api, "thinking_api": spec.thinking_api,
               "configured": bool(os.getenv(spec.key_env, "").strip()), "supports_thinking": spec.thinking_api != "none"}
              for name, spec in routing.config.models.items() if routing.config.compliant(spec, policy)]
    return {"models": models, "defaults": defaults}


@router.post("/ops/models/default")
async def set_model_default(state: Container, payload: ModelDefault, principal: Principal = Depends(require_platform_admin)):
    if not state.orchestrator:
        raise HTTPException(503, "service not ready")
    routing = state.orchestrator.router
    async with admin_change(state, principal, "model_default_changed", payload.route) as change:
        routing.reload()
        route = payload.route
        if payload.reset:
            routing.defaults.pop(route, None)
        else:
            if not payload.model:
                raise HTTPException(400, "缺少模型")
            level = payload.thinking or routing.config.routes[route].thinking
            try:
                routing.preferences(route, principal.tenant, payload.model, level)
                for tenant in routing.config.tenants:
                    routing.preferences(route, tenant, thinking=level)
            except PermissionError as exc:
                raise HTTPException(403, str(exc)) from None
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from None
            routing.defaults[route] = (payload.model, level)
        choices, level = routing.preferences(route, principal.tenant)
        change.detail = {"route": route, "model": choices[0], "thinking": level, "reset": payload.reset}
    return change.detail


@router.get("/readyz")
async def ready(state: Container):
    available = not state.draining and state.lifecycle and state.lifecycle.state in {"ready", "degraded"}
    return Response(status_code=200 if available else 503)


@router.get("/livez")
@router.get("/health")
async def health(state: Container):
    return {"status": "ok", "lifecycle": state.lifecycle.state if state.lifecycle else "uninitialized", "service": "ServiceDesk Agent", "port": int(os.getenv("SERVICEDESK_AGENT_PORT", "8001"))}


@router.get("/skills")
async def skills(state: Container):
    return state.skills.summary() if state.skills else {"count": 0, "skills": []}


@router.post("/skills/reload")
async def reload_skills(state: Container, principal: Principal = Depends(require_platform_admin)):
    async with admin_change(state, principal, "skills_reloaded") as change:
        body = state.skills.summary() if state.skills and state.skills.reload() is not None else {"count": 0}
        change.detail = {"count": body.get("count", 0)}
    return body


@router.get("/metrics")
async def prometheus_metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def _owns_trace(trace: Optional[Dict[str, Any]], principal: Principal) -> bool:
    scope = (trace or {}).get("observability") or {}
    return scope.get('tenant') == principal.tenant and (principal.can_read_all or scope.get('owner') == principal.subject)


# 静态路径必须在 /trace/{request_id} 之前注册，否则会被当作 request_id 匹配。
@router.get("/trace/tools")
async def trace_tools(state: Container, limit: int = 20, principal: Principal = Depends(require_roles(ROLE_IT_ADMIN, ROLE_AUDITOR))):
    rows = await state.memory.recent_traces(200) if state.memory else []
    return {"items": [r for r in rows if _owns_trace(r, principal)][:max(1, min(limit, 200))]}


async def _readable_trace(state: AppContainer, request_id: str, principal: Principal):
    trace = await state.memory.get_trace(request_id) if state.memory else None
    if trace is not None and not _owns_trace(trace, principal):
        raise forbid("只能查看自己的请求 trace")
    return trace


@router.get("/trace/tool/{request_id}")
async def trace_tool(state: Container, request_id: str, principal: Principal = Depends(current_principal)):
    trace = await _readable_trace(state, request_id, principal)
    return {"request_id": request_id, "found": trace is not None, "trace": trace or {}}


@router.get("/trace/{request_id}")
async def trace_request(state: Container, request_id: str, principal: Principal = Depends(current_principal)):
    """Canonical replay endpoint for plan, tools, memory and approval events (owner, admin or auditor only)."""
    trace = await _readable_trace(state, request_id, principal)
    return {"request_id": request_id, "found": trace is not None, "trace": trace or {}, "schema_version": 1}


@router.post("/ops/retention/run")
async def run_retention(state: Container, principal: Principal = Depends(require_platform_admin)):
    """立即执行一次保留期清理（后台每小时也会执行）；返回各类删除行数。审批、执行与审计记录不会被清理。"""
    if state.retention is None:
        raise HTTPException(503, "service not ready")
    async with admin_change(state, principal, "retention_run") as change:
        deleted = await state.retention.sweep_async()
        change.detail = deleted
    return {"deleted": deleted, "policy": {
        "episodic_days": state.retention.policy.episodic_days, "turn_days": state.retention.policy.turn_days, "history_days": state.retention.policy.history_days}}


@router.get("/admin/audit")
async def admin_audit_log(state: Container, principal: Principal = Depends(require_roles(ROLE_IT_ADMIN, ROLE_AUDITOR)),
                          limit: int = Query(100, ge=1, le=500), before: Optional[int] = Query(None, ge=1),
                          event: Optional[str] = Query(None, max_length=60),
                          outcome: Optional[str] = Query(None, pattern="^(succeeded|failed|interrupted)$")):
    """管理操作审计（知识库增删、策略生效与复核、技能重载、数据清理、令牌吊销）；本租户加全局策略事件，新到旧，按 before 翻页。

    默认列出成功的修改和中断的修改（已开始、没有结果，可能已部分生效，需要核对）；失败的尝试用 outcome=failed 查看。"""
    from servicedesk_agent.infra.admin_audit import recent

    if state.workflow is None:
        raise HTTPException(503, "service not ready")
    items = await state.workflow.run_with_engine(recent, principal.tenant, limit=limit, before=before, event=event, outcome=outcome)
    return {"items": items, "next_before": items[-1]["id"] if len(items) == limit else None}


@router.get("/admin/changes")
async def behavior_changes(state: Container, since: Optional[float] = Query(None, ge=0), kind: Optional[str] = None,
                           principal: Principal = Depends(require_roles(ROLE_IT_ADMIN, ROLE_AUDITOR))):
    from sqlalchemy import or_, select
    from servicedesk_agent.infra.db import change_events, read
    if state.workflow is None:
        raise HTTPException(503, "service not ready")
    def fetch():
        query = select(change_events).where(or_(change_events.c.tenant == principal.tenant,
                                                change_events.c.tenant.is_(None)))
        if since is not None:
            query = query.where(change_events.c.at >= since)
        if kind is not None:
            query = query.where(change_events.c.kind == kind)
        with read(state.workflow.engine) as db:
            return [dict(r) for r in db.execute(query.order_by(change_events.c.at.desc()).limit(500)).mappings()]
    return {"items": await state.workflow.run(fetch)}
