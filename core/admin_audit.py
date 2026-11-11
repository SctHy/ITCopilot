"""Append-only record of administrative changes (who changed the knowledge base, re-ran the
policy check, reloaded skills, swept retained data), separate from per-approval ``audit``.

Recorded after the change succeeds; a failed change leaves no entry. Entries are never
updated or deleted, and the retention sweep does not touch them.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from sqlalchemy import insert, select

from core import db as schema
from core.db import admin_audit
from core.identity import Principal


def record(engine, principal: Principal, event: str, subject: Optional[str] = None,
           detail: Optional[Dict[str, Any]] = None, *, at: Optional[float] = None) -> None:
    with engine.begin() as db:
        db.execute(insert(admin_audit).values(tenant=principal.tenant, actor=principal.subject[:200], event=event[:60],
                                              subject=(subject or None) and subject[:200], detail=detail, at=at or time.time()))


def recent(engine, tenant: str, *, limit: int = 100, before: Optional[int] = None, event: Optional[str] = None) -> List[Dict[str, Any]]:
    """Newest first; ``before`` is the id of the last entry already seen (keyset paging)."""
    query = select(admin_audit).where(admin_audit.c.tenant == tenant)
    if before is not None:
        query = query.where(admin_audit.c.id < before)
    if event:
        query = query.where(admin_audit.c.event == event)
    with schema.read(engine) as db:
        rows = db.execute(query.order_by(admin_audit.c.id.desc()).limit(limit)).mappings().all()
    return [{"id": r["id"], "actor": r["actor"], "event": r["event"], "subject": r["subject"], "detail": r["detail"],
             "at": r["at"]} for r in rows]
