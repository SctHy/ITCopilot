"""A failed policy event cannot leave an audit marker that suppresses its retry."""
import asyncio

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DatabaseError

from it_copilot.infra.db import admin_audit, change_events
from it_copilot.infra.tenant_context import system_scope
from it_copilot.workflow.adapters import AdapterRegistry
from it_copilot.workflow.adapters.local import LocalDirectoryAdapter
from it_copilot.workflow.store import WorkflowStore
from it_copilot.workflow.worker import ExecutionWorker


def reject_policy_changes(engine):
    condition = "NEW.kind = 'policy' AND NEW.tenant IS NULL"
    with engine.begin() as db:
        if db.dialect.name == "sqlite":
            db.execute(text(f"CREATE TRIGGER reject_policy_event BEFORE INSERT ON change_events WHEN {condition} "
                            "BEGIN SELECT RAISE(ABORT, 'blocked policy event'); END"))
        else:
            db.execute(text("CREATE FUNCTION reject_policy_event_fn() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
                            f"IF {condition} THEN RAISE EXCEPTION 'blocked policy event'; END IF; RETURN NEW; END $$"))
            db.execute(text("CREATE TRIGGER reject_policy_event BEFORE INSERT ON change_events FOR EACH ROW "
                            "EXECUTE FUNCTION reject_policy_event_fn()"))


def allow_policy_changes(engine):
    with engine.begin() as db:
        db.execute(text("DROP TRIGGER reject_policy_event" + (" ON change_events" if db.dialect.name == "postgresql" else "")))
        if db.dialect.name == "postgresql":
            db.execute(text("DROP FUNCTION reject_policy_event_fn()"))


def counts(engine):
    with system_scope(), engine.connect() as db:
        audits = db.scalar(select(func.count()).select_from(admin_audit).where(
            admin_audit.c.tenant == "*", admin_audit.c.event == "policy_loaded"))
        events = db.scalar(select(func.count()).select_from(change_events).where(change_events.c.kind == "policy",
            change_events.c.tenant.is_(None)))
    return audits, events


def test_policy_audit_and_change_event_commit_together_and_same_worker_retries(db_url):
    store = WorkflowStore(db_url)
    worker = ExecutionWorker(store, AdapterRegistry(LocalDirectoryAdapter(store.engine)), worker_id="atomic-test")
    reject_policy_changes(store.engine)
    with pytest.raises(DatabaseError, match="blocked policy event"):
        asyncio.run(worker._revalidate_on_policy_change())
    assert counts(store.engine) == (0, 0)
    allow_policy_changes(store.engine)
    asyncio.run(worker._revalidate_on_policy_change())
    assert counts(store.engine) == (1, 1)
    asyncio.run(worker._revalidate_on_policy_change())
    assert counts(store.engine) == (1, 1)
    store.close()
