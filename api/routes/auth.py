"""登录方式与开发令牌。"""
import os

from fastapi import APIRouter, Depends, HTTPException

import asyncio

from fastapi import Query
from sqlalchemy import select

from api import state
from api.deps import current_principal, record_admin_change, require_admin, require_roles
from api.schemas import DevTokenRequest, SessionRevocationRequest
from core import db as schema
from core.identity import ROLE_AUDITOR, ROLE_IT_ADMIN, AuthError, Principal, dev_mode_allowed, dev_users, issue_dev_token

router = APIRouter()


@router.get("/auth/config")
async def auth_config():
    """前端登录方式：dev 模式列出演示身份；oidc 模式给出 Keycloak 地址与 client。"""
    if dev_mode_allowed():
        return {"mode": "dev", "users": [{"username": u, "department": d.get("department"), "roles": d.get("roles", []),
                                          "employee_id": d.get("employee_id")} for u, d in dev_users().items()]}
    return {"mode": os.getenv("IT_COPILOT_AUTH_MODE", "dev"), "issuer": os.getenv("OIDC_PUBLIC_ISSUER") or os.getenv("OIDC_ISSUER"),
            "client_id": os.getenv("OIDC_FRONTEND_CLIENT_ID", "it-copilot-web")}


@router.post("/auth/dev-token")
async def dev_token(payload: DevTokenRequest):
    if not dev_mode_allowed():
        raise HTTPException(404, "not found")
    try:
        return {"access_token": issue_dev_token(payload.username), "token_type": "Bearer"}
    except AuthError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/auth/me")
async def auth_me(principal: Principal = Depends(current_principal)):
    return principal.public()


def _sessions():
    if state.sessions is None:
        raise HTTPException(503, "service not ready")
    return state.sessions


@router.post("/auth/logout-all")
async def logout_everywhere(principal: Principal = Depends(current_principal)):
    """让本人此前签发的全部令牌失效（含其他设备）；之后需重新登录。IdP 会话需另行退出。"""
    watermark = await asyncio.to_thread(_sessions().revoke, principal.tenant, principal.subject, actor=principal.subject, reason="logout-all")
    await record_admin_change(principal, "sessions_revoked", principal.subject, {"self": True, "revoked_before": watermark})
    return {"subject": principal.subject, "revoked_before": watermark}


def _resolve_subject(tenant: str, username: str) -> str:
    from core.db import task_identities  # identity snapshots of people who signed in

    with schema.read(state.workflow.engine) as db:
        subjects = db.execute(select(task_identities.c.subject).where(
            task_identities.c.tenant == tenant, task_identities.c.profile["username"].as_string() == username)).scalars().all()
    if not subjects:
        raise HTTPException(404, "本租户没有该用户名的登录记录；请改用 subject")
    if len(subjects) > 1:
        raise HTTPException(409, "该用户名对应多个身份；请改用 subject")
    return subjects[0]


@router.post("/admin/sessions/revoke")
async def revoke_sessions(payload: SessionRevocationRequest, principal: Principal = Depends(require_admin)):
    """立即吊销某人此前签发的全部令牌（离职、令牌泄露）；只作用于本租户。离职还需在 IdP 中停用账号。"""
    if bool(payload.subject) == bool(payload.username):
        raise HTTPException(422, "subject 与 username 需且仅需提供一个")
    if state.workflow is None:
        raise HTTPException(503, "service not ready")
    subject = payload.subject or await asyncio.to_thread(_resolve_subject, principal.tenant, payload.username)
    watermark = await asyncio.to_thread(_sessions().revoke, principal.tenant, subject, actor=principal.subject, reason=payload.reason)
    await record_admin_change(principal, "sessions_revoked", subject, {"reason": payload.reason, "revoked_before": watermark})
    return {"subject": subject, "revoked_before": watermark}


@router.get("/admin/sessions/revocations")
async def list_revocations(principal: Principal = Depends(require_roles(ROLE_IT_ADMIN, ROLE_AUDITOR)),
                           limit: int = Query(100, ge=1, le=500)):
    return {"items": await asyncio.to_thread(_sessions().list, principal.tenant, limit)}
