"""Which tenant the current code acts for (ADR 0023).

Set once at each entry - an authenticated request (``api.deps.current_principal``), an IM turn,
a worker running one action or one task run - and read wherever the tenant is needed but not
passed: the database session setting row-level security filters on (``core.db``), and the
sandbox directory behind the local adapter.

``SYSTEM`` is the scope of background jobs that work across tenants (claiming the outbox,
expiring approvals, retention). It is explicit: code that never entered a scope has none, and
``require_tenant`` / row-level security refuse it instead of guessing.

Context variables follow ``await`` and are copied into ``asyncio.to_thread`` and new tasks, so a
scope entered in a coroutine covers the work it starts. Plain ``threading.Thread`` targets start
without one.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

SYSTEM = "*"

_current: ContextVar[Optional[str]] = ContextVar("it_copilot_tenant", default=None)


class TenantContextMissing(RuntimeError):
    """Tenant-owned data was touched outside any tenant scope."""


def current_tenant() -> Optional[str]:
    """The tenant in scope, ``SYSTEM``, or None outside any scope."""
    return _current.get()


def require_tenant() -> str:
    """The tenant in scope; raises in system scope or outside any scope."""
    tenant = _current.get()
    if not tenant or tenant == SYSTEM:
        raise TenantContextMissing("this operation needs a tenant scope")
    return tenant


@contextmanager
def tenant_scope(tenant: str) -> Iterator[str]:
    if not tenant:
        raise ValueError("tenant is required")
    token = _current.set(tenant)
    try:
        yield tenant
    finally:
        _current.reset(token)


@contextmanager
def system_scope() -> Iterator[str]:
    token = _current.set(SYSTEM)
    try:
        yield SYSTEM
    finally:
        _current.reset(token)


def enter(tenant: str) -> None:
    """Set the scope for the rest of the current context (a request's task); no reset needed."""
    if not tenant:
        raise ValueError("tenant is required")
    _current.set(tenant)
