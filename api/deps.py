"""路由共用的依赖：身份解析、角色检查、请求 ID 与后台任务。"""
import asyncio
import logging
import math
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastapi import Depends, Header, HTTPException

from api import state
from core.identity import ROLE_IT_ADMIN, AuthError, Principal, principal_from_token
from monitor import metrics

logger = logging.getLogger("it_copilot")


def spawn(coro) -> None:
    task = asyncio.create_task(coro)
    state.background_tasks.add(task)
    task.add_done_callback(_on_background_done)


def _on_background_done(task: asyncio.Task) -> None:
    state.background_tasks.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.warning("background task failed: %s", type(task.exception()).__name__)


async def rate_limit_decision(principal: Principal, bucket: str, channel: str):
    """Shared check for every entry that starts model work; returns the decision, never raises."""
    from core.rate_limit import Decision

    if state.rate_limiter is None:
        return Decision(True)
    decision = await state.rate_limiter.check(bucket, principal.scope_key)
    if not decision.allowed:
        metrics.RATE_LIMITED.labels(bucket=bucket, channel=channel).inc()
    return decision


async def enforce_rate_limit(principal: Principal, bucket: str, channel: str = "http") -> None:
    decision = await rate_limit_decision(principal, bucket, channel)
    if not decision.allowed:
        retry = max(1, math.ceil(decision.retry_after_s))
        raise HTTPException(429, f"请求过于频繁，请 {retry} 秒后再试", headers={"Retry-After": str(retry)})


class AdminChange:
    """What the audit entry will say once the change finished; the route fills in results."""

    def __init__(self, subject: Optional[str], detail: Optional[Dict[str, Any]]):
        self.subject, self.detail = subject, dict(detail or {})


@asynccontextmanager
async def admin_change(principal: Principal, event: str, subject: Optional[str] = None, detail: Optional[Dict[str, Any]] = None):
    """Audit an administrative change write-ahead (``core.admin_audit``): ``started`` before the body
    runs, ``succeeded`` / ``failed`` after. If the started entry cannot be written the change does
    not run - an unaudited admin change is worse than a refused one. No-op without a database."""
    change = AdminChange(subject, detail)
    if state.workflow is None:
        yield change
        return
    from core import admin_audit

    engine = state.workflow.engine
    ref = await asyncio.to_thread(admin_audit.begin, engine, principal, event, subject, detail)
    try:
        yield change
    except Exception as exc:
        reason = exc.detail if isinstance(exc, HTTPException) else type(exc).__name__
        await asyncio.to_thread(admin_audit.finish, engine, ref, principal, event, ok=False, subject=change.subject,
                                detail={**change.detail, "error": str(reason)[:300]})
        raise
    await asyncio.to_thread(admin_audit.finish, engine, ref, principal, event, ok=True, subject=change.subject,
                            detail=change.detail or None)


def check_tenant(tenant: str) -> None:
    """Multi-tenant mode (ADR 0022): the token's tenant must be registered and active. Fails closed."""
    from core.tenancy import TenantUnavailable

    try:
        state.tenants.check(tenant)
    except TenantUnavailable as exc:
        if exc.reason == "unknown":
            metrics.AUTH_FAILURES.labels(reason="unknown_tenant").inc()
            raise HTTPException(401, "租户未开通", headers={"WWW-Authenticate": 'Bearer error="invalid_token"'}) from None
        metrics.AUTH_FAILURES.labels(reason="tenant_inactive").inc()
        raise HTTPException(403, "租户已停用，请联系平台管理员") from None
    except Exception:
        logger.warning("tenant registry lookup failed", exc_info=True)
        raise HTTPException(503, "身份校验暂不可用，请稍后重试") from None


def current_principal(authorization: Optional[str] = Header(None)) -> Principal:
    scheme, _, token = (authorization or "").partition(" ")
    try:
        if scheme.lower() != "bearer":
            raise AuthError("missing bearer token")
        principal = principal_from_token(token.strip())
    except AuthError as exc:
        metrics.AUTH_FAILURES.labels(reason="unauthenticated").inc()
        raise HTTPException(401, str(exc), headers={"WWW-Authenticate": "Bearer"}) from None
    if state.tenants is not None:
        check_tenant(principal.tenant)
    if state.sessions is not None:
        try:
            revoked = state.sessions.is_revoked(principal)
        except Exception:
            # Fail closed: this is an authentication decision, not a capacity control.
            logger.warning("session revocation lookup failed", exc_info=True)
            raise HTTPException(503, "身份校验暂不可用，请稍后重试") from None
        if revoked:
            metrics.AUTH_FAILURES.labels(reason="revoked").inc()
            raise HTTPException(401, "会话已被撤销，请重新登录", headers={"WWW-Authenticate": 'Bearer error="invalid_token"'})
    access = getattr(state.task_capabilities, "access", None) if state.task_capabilities else None
    if access:
        try:
            access.observe(principal)  # directory upkeep for task invites; must not gate unrelated endpoints
        except Exception:
            logger.warning("task identity observe failed; retried on next request", exc_info=True)
    return principal


def require_roles(*roles: str):
    def dependency(principal: Principal = Depends(current_principal)) -> Principal:
        if not principal.has_any(*roles):
            metrics.AUTH_FAILURES.labels(reason="forbidden").inc()
            raise HTTPException(403, f"需要以下角色之一：{', '.join(roles)}")
        return principal
    return dependency


require_admin = require_roles(ROLE_IT_ADMIN)


def forbid(message: str = "无权访问该资源"):
    metrics.AUTH_FAILURES.labels(reason="forbidden").inc()
    # 404 instead of 403 would hide existence; 403 is clearer for this internal tool.
    return HTTPException(403, message)


def request_id(principal: Principal, client_request_id: Optional[str]) -> str:
    """客户端幂等键按调用者隔离：不同用户使用相同 client_request_id 不会共享回执、trace 或审批。"""
    if not client_request_id:
        return str(uuid.uuid4())
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"it-copilot:v2:{principal.scope_key}:{client_request_id}"))


def env_flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).lower() in {"1", "true", "yes"}


def require_workflow():
    if not state.workflow:
        raise HTTPException(503, "approval store unavailable")
    return state.workflow
