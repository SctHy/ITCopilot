"""路由共用的依赖：身份解析、角色检查、请求 ID 与后台任务。"""
import asyncio
import logging
import os
import uuid
from typing import Optional

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


def current_principal(authorization: Optional[str] = Header(None)) -> Principal:
    scheme, _, token = (authorization or "").partition(" ")
    try:
        if scheme.lower() != "bearer":
            raise AuthError("missing bearer token")
        return principal_from_token(token.strip())
    except AuthError as exc:
        metrics.AUTH_FAILURES.labels(reason="unauthenticated").inc()
        raise HTTPException(401, str(exc), headers={"WWW-Authenticate": "Bearer"}) from None


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
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"it-copilot:{principal.tenant}:{principal.subject}:{client_request_id}"))


def env_flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).lower() in {"1", "true", "yes"}


def require_workflow():
    if not state.workflow:
        raise HTTPException(503, "approval store unavailable")
    return state.workflow
