"""PostgreSQL row-level security (migration 0005, ADR 0023).

Runs only with IT_COPILOT_TEST_DATABASE_URL. The test connection is usually a superuser, and
superusers bypass every policy, so the assertions run under ``SET ROLE`` to a plain probe role.
"""
import os
from dataclasses import replace

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from core import admin_audit
from core.tenant_context import SYSTEM, system_scope, tenant_scope
from tests.test_workflow import ALICE, ERIN, access
from workflow.store import WorkflowStore
from workflow.tasks import TaskStore

pytestmark = pytest.mark.skipif(not os.getenv("IT_COPILOT_TEST_DATABASE_URL"), reason="needs PostgreSQL (IT_COPILOT_TEST_DATABASE_URL)")

PROBE = "itc_rls_probe"
A, B = replace(ALICE, tenant="acme"), replace(ALICE, tenant="globex")


@pytest.fixture
def world(db_url):
    store = WorkflowStore(db_url)
    tasks = TaskStore(store.engine)
    for principal in (A, B):
        with tenant_scope(principal.tenant):
            store.create(f"ap-{principal.tenant}", request_id=f"r-{principal.tenant}", conv_id="c", principal=principal,
                         action_requests=[access()])
            tasks.create(principal, f"{principal.tenant} 事项", "需要处理", "k")
            admin_audit.record(store.engine, replace(ERIN, tenant=principal.tenant), "knowledge_added", principal.tenant)
    with system_scope():
        admin_audit.record_system(store.engine, "policy_loaded", "v1")
    with store.engine.begin() as db:
        schema = db.execute(text("SELECT current_schema()")).scalar()
        if not db.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": PROBE}).first():
            db.execute(text(f"CREATE ROLE {PROBE} NOLOGIN NOSUPERUSER NOBYPASSRLS"))
        db.execute(text(f"GRANT USAGE ON SCHEMA {schema} TO {PROBE}"))
        db.execute(text(f"GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA {schema} TO {PROBE}"))
        db.execute(text(f"GRANT USAGE ON ALL SEQUENCES IN SCHEMA {schema} TO {PROBE}"))
    yield store
    store.close()


def as_probe(store, tenant, sql, **params):
    """Run ``sql`` as the plain role with ``app.tenant`` = ``tenant`` (None: no scope)."""
    with store.engine.connect() as db:
        with db.begin():
            db.execute(text(f"SET LOCAL ROLE {PROBE}"))
            db.execute(text("SELECT set_config('app.tenant', :t, true)"), {"t": tenant or ""})
            result = db.execute(text(sql), params)
            return result.all() if result.returns_rows else result.rowcount


def tenants_in(store, tenant, table):
    return sorted({r[0] for r in as_probe(store, tenant, f"SELECT tenant FROM {table}")})


def test_each_scope_sees_only_its_own_rows(world):
    for table in ("approvals", "service_tasks", "audit"):
        assert tenants_in(world, "acme", table) == ["acme"], table
        assert tenants_in(world, "globex", table) == ["globex"], table
        assert tenants_in(world, SYSTEM, table) == ["acme", "globex"], table
        assert tenants_in(world, None, table) == [], table                 # no scope: nothing, not everything
    events = as_probe(world, "acme", "SELECT t.tenant FROM task_events e JOIN service_tasks t ON t.id = e.task_id")
    assert {r[0] for r in events} == {"acme"}
    assert as_probe(world, "acme", "SELECT count(*) FROM task_events")[0][0] == len(events)   # children follow the room


def test_global_audit_entries_are_readable_from_every_tenant_but_not_writable(world):
    assert tenants_in(world, "acme", "admin_audit") == ["*", "acme"]
    assert tenants_in(world, None, "admin_audit") == []
    with pytest.raises(DBAPIError, match="row-level security"):
        as_probe(world, "acme", "INSERT INTO admin_audit (tenant, actor, event, at) VALUES ('*', 'x', 'policy_loaded', 0)")


def test_a_scope_cannot_write_or_move_rows_into_another_tenant(world):
    with pytest.raises(DBAPIError, match="row-level security"):
        as_probe(world, "acme", "INSERT INTO directory_employees (tenant, id, username, active, locked, password_version) "
                                "VALUES ('globex', 'x', 'x', 1, 0, 0)")
    with pytest.raises(DBAPIError, match="row-level security"):
        as_probe(world, "acme", "UPDATE approvals SET tenant = 'globex' WHERE id = 'ap-acme'")
    assert as_probe(world, "acme", "UPDATE approvals SET status = 'rejected' WHERE id = 'ap-globex'") == 0


def test_audit_for_an_approval_outside_the_scope_is_refused(world):
    with pytest.raises((IntegrityError, DBAPIError)):
        as_probe(world, "acme", "INSERT INTO audit (tenant, approval_id, event, actor, at) "
                                "VALUES ((SELECT tenant FROM approvals WHERE id = 'ap-globex'), 'ap-globex', 'created', 'x', 0)")


def test_pooled_connections_do_not_keep_the_previous_tenant(world):
    with tenant_scope("acme"), world.engine.connect() as db:
        assert db.execute(text("SELECT current_setting('app.tenant', true)")).scalar() == "acme"
        db.commit()
    with world.engine.connect() as db:   # same pool, no scope
        assert db.execute(text("SELECT current_setting('app.tenant', true)")).scalar() == ""
