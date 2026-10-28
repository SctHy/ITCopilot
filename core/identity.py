"""Server-side caller identity.

Every request is mapped to a ``Principal`` derived from a verified JWT. Fields a
client could forge (user, department, approver name) are never read from request
bodies. Two token sources share one verification path:

- ``oidc``: RS256 tokens from an OpenID Connect provider (Keycloak in compose),
  verified against the provider's JWKS, issuer and audience.
- ``dev``: HS256 tokens minted by this process for a fixed demo directory
  (``config/dev_users.yaml``). Used by tests, offline evaluation and the local UI;
  refused when ``IT_COPILOT_ENV=production``.
"""
from __future__ import annotations

import os
import pathlib
import secrets
import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Dict, FrozenSet, Iterable, Optional

import jwt
import yaml

ROLE_EMPLOYEE = "employee"
ROLE_APPROVER = "approver"
ROLE_IT_ADMIN = "it_admin"
ROLE_AUDITOR = "auditor"
KNOWN_ROLES = frozenset({ROLE_EMPLOYEE, ROLE_APPROVER, ROLE_IT_ADMIN, ROLE_AUDITOR})

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_DEV_ISSUER = "it-copilot-dev"
_DEV_AUDIENCE = "it-copilot"
# A process-local secret keeps dev tokens from being replayable across restarts unless
# the operator pins IT_COPILOT_DEV_JWT_SECRET (needed when several workers share tokens).
_PROCESS_SECRET = secrets.token_urlsafe(32)


class AuthError(Exception):
    """Token missing, malformed, expired or signed by an untrusted key."""


@dataclass(frozen=True)
class Principal:
    subject: str
    username: str
    department: str
    roles: FrozenSet[str] = field(default_factory=frozenset)
    employee_id: Optional[str] = None
    tenant: str = "default"

    def has_any(self, *roles: str) -> bool:
        return bool(self.roles.intersection(roles))

    @property
    def is_admin(self) -> bool:
        return ROLE_IT_ADMIN in self.roles

    @property
    def can_read_all(self) -> bool:
        """Operators and auditors may read other users' traces, approvals and audit logs."""
        return self.has_any(ROLE_IT_ADMIN, ROLE_AUDITOR)

    def public(self) -> Dict[str, Any]:
        return {
            "subject": self.subject, "username": self.username, "department": self.department,
            "roles": sorted(self.roles), "employee_id": self.employee_id, "tenant": self.tenant,
        }


def auth_mode() -> str:
    return os.getenv("IT_COPILOT_AUTH_MODE", "dev").strip().lower()


def _dev_secret() -> str:
    return os.getenv("IT_COPILOT_DEV_JWT_SECRET") or _PROCESS_SECRET


def dev_mode_allowed() -> bool:
    return auth_mode() == "dev" and os.getenv("IT_COPILOT_ENV", "development").lower() != "production"


@lru_cache(maxsize=4)
def _load_dev_users(path: str) -> Dict[str, Dict[str, Any]]:
    data = yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8")) or {}
    users = {}
    for entry in data.get("users", []):
        unknown = set(entry.get("roles", [])) - KNOWN_ROLES
        if unknown:
            raise ValueError(f"dev user {entry.get('username')} has unknown roles: {sorted(unknown)}")
        users[entry["username"]] = entry
    return users


def dev_users() -> Dict[str, Dict[str, Any]]:
    return _load_dev_users(os.getenv("IT_COPILOT_DEV_USERS", str(_ROOT / "config/dev_users.yaml")))


def issue_dev_token(username: str, *, ttl_s: int = 8 * 3600, overrides: Optional[Dict[str, Any]] = None) -> str:
    """Mint a token for a demo-directory user. Overrides exist for tests that need
    an identity outside the directory (e.g. a per-case employee id in evaluation)."""
    if not dev_mode_allowed():
        raise AuthError("dev tokens are disabled")
    user = dict(dev_users().get(username) or {})
    if not user and not overrides:
        raise AuthError(f"unknown dev user: {username}")
    user.update(overrides or {})
    now = int(time.time())
    claims = {
        "iss": _DEV_ISSUER, "aud": _DEV_AUDIENCE, "iat": now, "exp": now + ttl_s,
        "sub": user.get("subject") or f"dev:{username}", "preferred_username": username,
        "department": user.get("department", "engineering"), "employee_id": user.get("employee_id"),
        "tenant": user.get("tenant", "default"), "roles": list(user.get("roles", [ROLE_EMPLOYEE])),
    }
    return jwt.encode(claims, _dev_secret(), algorithm="HS256")


@lru_cache(maxsize=4)
def _jwks_client(url: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(url, cache_keys=True, lifespan=600)


def _oidc_claims(token: str) -> Dict[str, Any]:
    issuer = os.getenv("OIDC_ISSUER", "").rstrip("/")
    if not issuer:
        raise AuthError("OIDC_ISSUER is not configured")
    jwks_url = os.getenv("OIDC_JWKS_URL") or f"{issuer}/protocol/openid-connect/certs"
    audience = os.getenv("OIDC_AUDIENCE", "it-copilot")
    try:
        key = _jwks_client(jwks_url).get_signing_key_from_jwt(token).key
        # Keycloak access tokens put the client in azp; aud only contains it when an
        # audience mapper is configured. Require one of the two to match.
        claims = jwt.decode(token, key, algorithms=["RS256", "ES256"], issuer=issuer, options={"verify_aud": False, "require": ["exp", "iat", "sub"]})
    except jwt.PyJWTError as exc:
        raise AuthError(f"invalid token: {type(exc).__name__}") from exc
    aud = claims.get("aud")
    audiences = set(aud) if isinstance(aud, list) else {aud}
    if audience not in audiences and claims.get("azp") != audience:
        raise AuthError("token audience mismatch")
    return claims


def _dev_claims(token: str) -> Dict[str, Any]:
    try:
        return jwt.decode(token, _dev_secret(), algorithms=["HS256"], audience=_DEV_AUDIENCE, issuer=_DEV_ISSUER, options={"require": ["exp", "sub"]})
    except jwt.PyJWTError as exc:
        raise AuthError(f"invalid token: {type(exc).__name__}") from exc


def _roles(claims: Dict[str, Any]) -> FrozenSet[str]:
    raw: Iterable[str] = claims.get("roles") or (claims.get("realm_access") or {}).get("roles") or []
    # Providers add their own default roles (offline_access, uma_authorization ...);
    # only roles this service understands carry authority.
    return frozenset(r for r in raw if r in KNOWN_ROLES)


def principal_from_token(token: str) -> Principal:
    if not token:
        raise AuthError("missing bearer token")
    mode = auth_mode()
    if mode == "oidc":
        claims = _oidc_claims(token)
    elif mode == "dev":
        if not dev_mode_allowed():
            raise AuthError("dev authentication is disabled in production")
        claims = _dev_claims(token)
    else:
        raise AuthError(f"unsupported IT_COPILOT_AUTH_MODE={mode}")
    department = claims.get("department")
    if isinstance(department, list):
        department = department[0] if department else None
    if not department:
        # Department drives knowledge filtering and approval routing; a token without
        # it is not usable rather than silently defaulting to a broad scope.
        raise AuthError("token has no department claim")
    employee_id = claims.get("employee_id")
    if isinstance(employee_id, list):
        employee_id = employee_id[0] if employee_id else None
    return Principal(
        subject=str(claims["sub"]), username=str(claims.get("preferred_username") or claims["sub"]),
        department=str(department), roles=_roles(claims), employee_id=str(employee_id) if employee_id else None,
        tenant=str(claims.get("tenant") or "default"),
    )
