"""Tenant registry and the multi-tenant switch (ADR 0022).

``IT_COPILOT_MULTI_TENANT`` off (default): one organisation per deployment; the ``tenant`` claim
still scopes data, but the registry is not consulted and behaviour is what it was before.

On: every request's tenant must be registered and ``active``. An unknown tenant is refused as
unauthenticated (401, its tokens are not ours to honour); a suspended or offboarding one as
forbidden (403). A failed lookup refuses the request (fail closed), like token revocation. The
switch also refuses SQLite, which has no row-level security to back the application filters.

Lookups go through a short per-process cache (``cache_ttl_s``), so a suspension reaches every API
process within that window. Status changes are written to ``admin_audit`` under the tenant, in the
same transaction as the change.

Tenant ids are lowercase ``[a-z0-9_-]``; ``*`` (global audit entries) and ids starting with ``_``
(platform-owned data such as a shared knowledge library) are reserved.
"""
from __future__ import annotations

import re
import threading
import time
from enum import StrEnum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import IntegrityError

from core import db as schema
from core.db import admin_audit, tenants as T
from core.env import multi_tenant_enabled  # noqa: F401  (re-exported)
from core.identity import default_issuer

DEFAULT_TENANT = "default"
TENANT_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,79}")


class TenantStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"        # reversible: sign-ins refused, data kept
    OFFBOARDING = "offboarding"    # on the way out: export, revoke grants, delete


class TenantUnavailable(Exception):
    """The tenant may not use the service now; ``reason`` is ``unknown`` or the tenant's status."""

    def __init__(self, tenant: str, reason: str):
        super().__init__(f"tenant {tenant!r} is {reason}")
        self.tenant, self.reason = tenant, reason


class TenantExists(Exception):
    pass


def ensure_supported(database_url: str, engine=None) -> None:
    """Refuse to start multi-tenant on a database that cannot enforce row-level isolation: SQLite
    (no RLS), or a PostgreSQL role that bypasses it - superusers and BYPASSRLS roles ignore every
    policy, FORCE or not (ADR 0023). ``engine`` is checked when given."""
    if not multi_tenant_enabled():
        return
    if database_url.startswith("sqlite"):
        raise RuntimeError("IT_COPILOT_MULTI_TENANT requires PostgreSQL; SQLite is single-tenant only (ADR 0022)")
    if engine is not None and bypasses_row_security(engine):
        raise RuntimeError("IT_COPILOT_MULTI_TENANT: the database role is a superuser or has BYPASSRLS, so row-level "
                           "security would not apply; connect as a plain role (deploy/postgres/app-role.sql, ADR 0023)")


def bypasses_row_security(engine) -> bool:
    if engine.dialect.name != "postgresql":
        return False
    with engine.connect() as db:
        return bool(db.execute(text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")).scalar())


def valid_tenant_id(tenant: str) -> bool:
    return bool(TENANT_ID.fullmatch(tenant or ""))


class TenantRegistry:
    def __init__(self, engine, *, clock: Callable[[], float] = time.time, cache_ttl_s: float = 5.0):
        self.engine, self.clock, self.cache_ttl_s = engine, clock, cache_ttl_s
        self._cache: Dict[str, Tuple[float, Optional[Dict[str, Any]]]] = {}
        self._lock = threading.Lock()
        self._issuers: Optional[Tuple[float, Set[str]]] = None

    def get(self, tenant: str) -> Optional[Dict[str, Any]]:
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(tenant)
        if cached and now - cached[0] < self.cache_ttl_s:
            return cached[1]
        with schema.read(self.engine) as db:
            row = db.execute(select(T).where(T.c.id == tenant)).mappings().first()
        value = dict(row) if row else None
        with self._lock:
            if len(self._cache) > 10_000:
                self._cache.clear()
            self._cache[tenant] = (now, value)
        return value

    def check(self, tenant: str, issuer: Optional[str] = None) -> None:
        """Raise ``TenantUnavailable`` unless the tenant is registered and active, and - for a token
        identity - the token came from the tenant's issuer (its own realm, or the deployment's default
        issuer when it has none). A realm can therefore only speak for its own tenant (ADR 0023)."""
        row = self.get(tenant)
        if row is None:
            raise TenantUnavailable(tenant, "unknown")
        if row["status"] != TenantStatus.ACTIVE:
            raise TenantUnavailable(tenant, row["status"])
        if issuer is not None and issuer.rstrip("/") != (row["issuer"] or default_issuer()).rstrip("/"):
            raise TenantUnavailable(tenant, "issuer_mismatch")

    def issuers(self) -> Set[str]:
        """Issuers bound to active tenants: the extra realms OIDC verification trusts."""
        now = time.monotonic()
        with self._lock:
            cached = self._issuers
        if cached and now - cached[0] < self.cache_ttl_s:
            return cached[1]
        with schema.read(self.engine) as db:
            value = set(db.execute(select(T.c.issuer).where(T.c.issuer.is_not(None), T.c.status == TenantStatus.ACTIVE)).scalars())
        with self._lock:
            self._issuers = (now, value)
        return value

    def feishu_tenant(self, tenant_key: Optional[str]) -> Optional[str]:
        """The tenant bound to a Feishu workspace (event ``header.tenant_key``), if any."""
        if not tenant_key:
            return None
        with schema.read(self.engine) as db:
            return db.execute(select(T.c.id).where(T.c.feishu_tenant_key == tenant_key)).scalar()

    def list(self) -> List[Dict[str, Any]]:
        with schema.read(self.engine) as db:
            return [dict(r) for r in db.execute(select(T).order_by(T.c.id)).mappings()]

    def create(self, tenant: str, *, actor: str, tier: str = "standard", issuer: Optional[str] = None,
               feishu_tenant_key: Optional[str] = None, policy_ref: Optional[str] = None,
               quotas: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not valid_tenant_id(tenant):
            raise ValueError(f"invalid tenant id {tenant!r}: lowercase letters, digits, '_' or '-', not starting with '_'")
        now = self.clock()
        row = {"id": tenant, "status": TenantStatus.ACTIVE.value, "tier": tier, "issuer": issuer or None,
               "feishu_tenant_key": feishu_tenant_key or None, "policy_ref": policy_ref or None, "quotas": dict(quotas or {}),
               "created_at": now, "updated_at": now}
        try:
            with self.engine.begin() as db:
                db.execute(insert(T).values(**row))
                self._audit(db, tenant, actor, "tenant_created", {k: row[k] for k in ("tier", "issuer", "feishu_tenant_key", "policy_ref")}, now)
        except IntegrityError as exc:
            raise TenantExists(f"tenant {tenant!r}, its issuer or its Feishu key is already registered") from exc
        self._forget(tenant)
        return row

    def set_status(self, tenant: str, status: TenantStatus, *, actor: str, reason: str = "") -> Dict[str, Any]:
        status = TenantStatus(status)
        now = self.clock()
        with self.engine.begin() as db:
            before = db.execute(select(T.c.status).where(T.c.id == tenant)).scalar()
            if before is None:
                raise KeyError(tenant)
            if before != status:
                db.execute(update(T).where(T.c.id == tenant).values(status=status.value, updated_at=now))
                self._audit(db, tenant, actor, "tenant_status_changed", {"from": before, "to": status.value, "reason": reason[:500]}, now)
        self._forget(tenant)
        return self.get(tenant)

    def _forget(self, tenant: str) -> None:
        with self._lock:
            self._cache.pop(tenant, None)
            self._issuers = None

    @staticmethod
    def _audit(db, tenant: str, actor: str, event: str, detail: Dict[str, Any], at: float) -> None:
        # Same transaction as the change: the registry lives in the audit database, so there is no
        # window for a change without its entry (contrast admin_audit.begin/finish).
        db.execute(insert(admin_audit).values(tenant=tenant, actor=actor[:200], event=event, subject=tenant,
                                              detail=detail, at=at, outcome="succeeded"))
