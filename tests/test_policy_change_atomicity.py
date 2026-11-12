"""A failed policy event cannot leave an audit marker that suppresses its retry."""
import asyncio

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DatabaseError
import yaml

from core.db import admin_audit, change_events
from core.tenant_context import system_scope
from workflow.adapters import AdapterRegistry
from workflow.adapters.local import LocalDirectoryAdapter
from workflow.store import WorkflowStore
from workflow.worker import ExecutionWorker


def reject_policy_changes(engine, tenant):
    condition = "NEW.kind = 'policy' AND " + ("NEW.tenant IS NULL" if tenant is None else "NEW.tenant = 'acme'")
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


def counts(engine, tenant):
    with system_scope(), engine.connect() as db:
        audits = db.scalar(select(func.count()).select_from(admin_audit).where(
            admin_audit.c.tenant == ("*" if tenant is None else tenant), admin_audit.c.event == "policy_loaded"))
        events = db.scalar(select(func.count()).select_from(change_events).where(change_events.c.kind == "policy",
            change_events.c.tenant.is_(None) if tenant is None else change_events.c.tenant == tenant))
    return audits, events


@pytest.mark.parametrize("tenant", [None, "acme"], ids=["platform", "tenant-overlay"])
def test_policy_audit_and_change_event_commit_together_and_same_worker_retries(db_url, tmp_path, monkeypatch, tenant):
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    monkeypatch.setenv("IT_COPILOT_POLICY_DIR", str(policy_dir))
    if tenant is not None:
        (policy_dir / "acme.yaml").write_text(yaml.safe_dump({"version": "atomic-overlay", "approval_ttl_hours": 1}))
    store = WorkflowStore(db_url)
    worker = ExecutionWorker(store, AdapterRegistry(LocalDirectoryAdapter(store.engine)), worker_id="atomic-test")
    reject_policy_changes(store.engine, tenant)
    with pytest.raises(DatabaseError, match="blocked policy event"):
        asyncio.run(worker._revalidate_on_policy_change())
    assert counts(store.engine, tenant) == (0, 0)
    allow_policy_changes(store.engine)
    asyncio.run(worker._revalidate_on_policy_change())
    assert counts(store.engine, tenant) == (1, 1)
    asyncio.run(worker._revalidate_on_policy_change())
    assert counts(store.engine, tenant) == (1, 1)
    store.close()
