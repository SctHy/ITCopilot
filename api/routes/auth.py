"""登录方式与开发令牌。"""
import os

from fastapi import APIRouter, Depends, HTTPException

from api.deps import current_principal
from api.schemas import DevTokenRequest
from core.identity import AuthError, Principal, dev_mode_allowed, dev_users, issue_dev_token

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
