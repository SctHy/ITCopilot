"""Timestamped behavior changes share one durable write path."""
import time

from sqlalchemy import insert, select

from core.db import change_events
from core.tenant_context import system_scope, tenant_scope


def append_change(db, kind, ref, detail, *, tenant=None, actor="system", at=None):
    db.execute(insert(change_events).values(at=time.time() if at is None else at, tenant=tenant,
                                          kind=kind, ref=ref, detail=detail, actor=actor))


def record_change(engine, kind, ref, detail, *, tenant=None, actor="system"):
    with (system_scope() if tenant is None else tenant_scope(tenant)), engine.begin() as db:
        append_change(db, kind, ref, detail, tenant=tenant, actor=actor)


def record_deploy(engine, build_sha):
    with engine.begin() as db:
        last = db.scalar(select(change_events.c.ref).where(change_events.c.kind == "deploy")
                         .order_by(change_events.c.id.desc()).limit(1))
        if last != build_sha:
            append_change(db, "deploy", build_sha, {"previous": last})
