"""Append-only record of administrative changes (who changed the knowledge base, re-ran the
policy check, reloaded skills, swept retained data, which policy took effect), separate from
per-approval ``audit``.

A change spans stores the audit table is not in (vector store, skill files, several
transactions), so it cannot share one transaction with its entry. Instead it is written ahead:

1. ``begin`` appends a ``started`` row before the change runs;
2. ``finish`` appends ``succeeded`` or ``failed`` with ``ref`` pointing at it.

A crash between the change and its outcome therefore leaves a ``started`` row with no outcome,
which ``recent`` reports as ``interrupted`` once it is older than ``INTERRUPTED_AFTER_S`` -
"something may have changed, check it" - rather than losing the change from the trail.
``recent`` lists succeeded and interrupted changes; failed attempts are kept but only listed
when asked for (``outcome="failed"``), so a refused or no-op request does not read as a change.

Entries are never updated or deleted (database triggers, migration 0002), and the retention
sweep does not touch them. Policy changes are global and recorded under tenant ``*``, which
every tenant's admins see.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, exists, insert, or_, select
from sqlalchemy.orm import aliased

from core import db as schema
from core.db import admin_audit
from core.identity import Principal

GLOBAL_TENANT = "*"
SYSTEM_ACTOR = "system"
INTERRUPTED_AFTER_S = 300.0
OUTCOMES = ("succeeded", "failed", "interrupted")


def _insert(engine, *, tenant: str, actor: str, event: str, subject: Optional[str], detail: Optional[Dict[str, Any]],
            outcome: str, ref: Optional[int] = None, at: Optional[float] = None) -> int:
    with engine.begin() as db:
        result = db.execute(insert(admin_audit).values(
            tenant=tenant, actor=actor[:200], event=event[:60], subject=(subject or None) and subject[:200],
            detail=detail, at=at or time.time(), outcome=outcome, ref=ref))
        return int(result.inserted_primary_key[0])


def begin(engine, principal: Principal, event: str, subject: Optional[str] = None,
          detail: Optional[Dict[str, Any]] = None, *, at: Optional[float] = None) -> int:
    """Write-ahead entry for a change about to run; returns the id ``finish`` refers to."""
    return _insert(engine, tenant=principal.tenant, actor=principal.subject, event=event, subject=subject,
                   detail=detail, outcome="started", at=at)


def finish(engine, ref: int, principal: Principal, event: str, *, ok: bool, subject: Optional[str] = None,
           detail: Optional[Dict[str, Any]] = None, at: Optional[float] = None) -> int:
    return _insert(engine, tenant=principal.tenant, actor=principal.subject, event=event, subject=subject, detail=detail,
                   outcome="succeeded" if ok else "failed", ref=ref, at=at)


def record(engine, principal: Principal, event: str, subject: Optional[str] = None,
           detail: Optional[Dict[str, Any]] = None, *, at: Optional[float] = None) -> int:
    """One-step entry for something that already happened atomically (or is an observation)."""
    return _insert(engine, tenant=principal.tenant, actor=principal.subject, event=event, subject=subject,
                   detail=detail, outcome="succeeded", at=at)


def record_system(engine, event: str, subject: Optional[str] = None, detail: Optional[Dict[str, Any]] = None,
                  *, at: Optional[float] = None) -> int:
    """Global, non-human entry (e.g. the access policy that took effect)."""
    return _insert(engine, tenant=GLOBAL_TENANT, actor=SYSTEM_ACTOR, event=event, subject=subject, detail=detail,
                   outcome="succeeded", at=at)


def record_as(engine, tenant: str, actor: str, event: str, subject: Optional[str] = None,
              detail: Optional[Dict[str, Any]] = None, *, at: Optional[float] = None) -> int:
    """A non-human entry under one tenant (e.g. that tenant's policy overlay took effect)."""
    return _insert(engine, tenant=tenant, actor=actor, event=event, subject=subject, detail=detail, outcome="succeeded", at=at)


def last_tenant_event(engine, tenant: str, event: str) -> Optional[Dict[str, Any]]:
    with schema.read(engine) as db:
        row = db.execute(select(admin_audit).where(admin_audit.c.tenant == tenant, admin_audit.c.event == event)
                         .order_by(admin_audit.c.id.desc()).limit(1)).mappings().first()
    return dict(row) if row else None


def last_system(engine, event: str) -> Optional[Dict[str, Any]]:
    with schema.read(engine) as db:
        row = db.execute(select(admin_audit).where(admin_audit.c.tenant == GLOBAL_TENANT, admin_audit.c.event == event)
                         .order_by(admin_audit.c.id.desc()).limit(1)).mappings().first()
    return dict(row) if row else None


def recent(engine, tenant: str, *, limit: int = 100, before: Optional[int] = None, event: Optional[str] = None,
           outcome: Optional[str] = None, now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Newest first; ``before`` is the id of the last entry already seen (keyset paging).

    Default: succeeded and interrupted changes. ``outcome`` narrows to one of ``OUTCOMES``."""
    closing = aliased(admin_audit)
    closed = exists().where(closing.c.ref == admin_audit.c.id)
    stale = (now or time.time()) - INTERRUPTED_AFTER_S
    succeeded = or_(admin_audit.c.outcome.is_(None), admin_audit.c.outcome == "succeeded")
    interrupted = and_(admin_audit.c.outcome == "started", admin_audit.c.at <= stale, ~closed)
    shown = {"succeeded": succeeded, "failed": admin_audit.c.outcome == "failed", "interrupted": interrupted,
             None: or_(succeeded, interrupted)}[outcome]
    query = select(admin_audit).where(admin_audit.c.tenant.in_((tenant, GLOBAL_TENANT)), shown)
    if before is not None:
        query = query.where(admin_audit.c.id < before)
    if event:
        query = query.where(admin_audit.c.event == event)
    with schema.read(engine) as db:
        rows = db.execute(query.order_by(admin_audit.c.id.desc()).limit(limit)).mappings().all()
    return [{"id": r["id"], "actor": r["actor"], "event": r["event"], "subject": r["subject"], "detail": r["detail"],
             "at": r["at"], "outcome": "interrupted" if r["outcome"] == "started" else (r["outcome"] or "succeeded"),
             "global": r["tenant"] == GLOBAL_TENANT} for r in rows]
