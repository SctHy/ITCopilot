"""Resource ownership and overlapping grants run against either supported database."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from sqlalchemy import select

from core.db import actions
from core.db import directory_effects
from tests.test_workflow import ALICE, BOB, CAROL, Clock, access
from workflow.adapters import AdapterRegistry
from workflow.adapters.local import LocalDirectoryAdapter
from workflow.store import WorkflowStore
from workflow.worker import ExecutionWorker


def approve(store, name, role, hours):
    body = store.create(name, request_id="r-" + name, conv_id="conv-" + name,
                        principal=ALICE, action_requests=[access(role=role, duration_hours=hours)])
    for principal in (BOB, CAROL):
        store.decide(name, "approved", principal, body["payload_hash"])


@pytest.mark.parametrize("first_role", ["developer", "reporter"])
def test_overlapping_grants_keep_latest_coverage_without_inheriting_temporary_baseline(db_url, first_role):
    clock = Clock()
    store = WorkflowStore(db_url, clock=clock)
    worker = ExecutionWorker(store, AdapterRegistry(LocalDirectoryAdapter(store.engine)))
    approve(store, "a", first_role, 24)
    asyncio.run(worker.run_until_idle())
    approve(store, "b", "developer", 72)
    asyncio.run(worker.run_until_idle())
    ledger = store.employee("1001", "default")["access_ledger"]
    assert [row["status"] for row in ledger] == ["active", "active"]
    assert all(row["baseline_role"] is None for row in ledger)
    if first_role == "developer":
        assert store.get("b")["results"][0]["covered_by"] == store.get("a")["results"][0]["operation_id"]
        with store.engine.connect() as db:
            assert db.execute(select(directory_effects.c.operation_id)).scalars().all() == [store.get("a")["results"][0]["operation_id"]]
    clock.t += 24 * 3600 + 1
    asyncio.run(worker.run_until_idle())
    assert store.employee("1001", "default")["grants"][0]["role"] == "developer"
    assert store.get("a")["revocations"][0]["status"] == "succeeded"
    clock.t += 48 * 3600
    asyncio.run(worker.run_until_idle())
    assert store.employee("1001", "default")["grants"] == []


def test_two_workers_cannot_execute_apply_and_revoke_for_one_resource(db_url):
    clock = Clock()
    store = WorkflowStore(db_url, clock=clock)
    local = LocalDirectoryAdapter(store.engine)
    first = ExecutionWorker(store, AdapterRegistry(local))
    approve(store, "a", "reporter", 24)
    asyncio.run(first.run_until_idle())
    approve(store, "b", "developer", 72)
    clock.t += 24 * 3600 + 1
    entered, release = threading.Event(), threading.Event()

    class BlockingAdapter(LocalDirectoryAdapter):
        async def apply(self, action_id, action):
            entered.set()
            await asyncio.to_thread(release.wait, 10)
            return await super().apply(action_id, action)

    registry = AdapterRegistry(BlockingAdapter(store.engine))
    workers = [ExecutionWorker(store, registry, worker_id=str(i)) for i in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        running = pool.submit(asyncio.run, workers[0].run_once(limit=1))
        try:
            assert entered.wait(10)
            other = pool.submit(asyncio.run, workers[1].run_once(limit=1))
            assert other.result(timeout=10) == 0
            with store.engine.connect() as db:
                executing = db.execute(select(actions).where(actions.c.status == "executing")).mappings().all()
            assert len(executing) == 1
        finally:
            release.set()
        assert running.result(timeout=10) == 1


def test_cross_conversation_conflicts_remain_visible_without_replacement(db_url):
    store = WorkflowStore(db_url)
    for name in ("a", "b"):
        store.create(name, request_id=name, conv_id=name, principal=ALICE, action_requests=[access()])
    body = store.get("a")
    assert body["status"] == "pending"
    assert body["conflicts"] == [{"approval_id": "b", "status": "pending", "requester": ALICE.subject,
                                  "conv_channel": "web"}]
    store.create("c", request_id="c", conv_id="c", principal=ALICE, action_requests=[access(system="Jira")])
    listed = {item["approval_id"]: item for item in store.list(ALICE)}
    assert {name: [c["approval_id"] for c in item["conflicts"]] for name, item in listed.items()} == {"a": ["b"], "b": ["a"], "c": []}
    assert all(listed[name] == store.get(name) for name in listed)


def test_overlaps_preserve_permanent_baseline_when_shorter_upgrade_expires_first(db_url):
    from tests.conftest import run_in_tenant

    clock = Clock()
    store = WorkflowStore(db_url, clock=clock)
    local = LocalDirectoryAdapter(store.engine)
    run_in_tenant(local.apply("seed", access(role="readonly", system="gitlab")))
    worker = ExecutionWorker(store, AdapterRegistry(local))
    approve(store, "a", "reporter", 72)
    asyncio.run(worker.run_until_idle())
    approve(store, "b", "developer", 24)
    asyncio.run(worker.run_until_idle())
    clock.t += 24 * 3600 + 1
    asyncio.run(worker.run_until_idle())
    assert store.employee("1001", "default")["grants"][0]["role"] == "reporter"
    clock.t += 48 * 3600
    asyncio.run(worker.run_until_idle())
    assert store.employee("1001", "default")["grants"][0]["role"] == "readonly"


def test_a_missing_live_grant_cannot_cover_a_new_request_from_the_ledger_alone(db_url):
    from tests.conftest import run_in_tenant

    clock = Clock()
    store = WorkflowStore(db_url, clock=clock)
    local = LocalDirectoryAdapter(store.engine)
    worker = ExecutionWorker(store, AdapterRegistry(local))
    approve(store, "a", "developer", 24)
    asyncio.run(worker.run_until_idle())
    run_in_tenant(local.apply("external-delete", {"tool_name": "revoke_access", "arguments": {
        "employee_id": "1001", "system": "gitlab", "restore_to": None}}))
    approve(store, "b", "developer", 72)
    asyncio.run(worker.run_until_idle())
    assert store.employee("1001", "default")["grants"] == []
    assert store.get("b")["results"][0]["status"] == "manual_review"
    assert "covered_by" not in store.get("b")["results"][0]


def test_a_new_request_cannot_hide_an_external_upgrade_as_a_temporary_grant(db_url):
    from tests.conftest import run_in_tenant

    clock = Clock()
    store = WorkflowStore(db_url, clock=clock)
    local = LocalDirectoryAdapter(store.engine)
    worker = ExecutionWorker(store, AdapterRegistry(local))
    approve(store, "a", "reporter", 24)
    asyncio.run(worker.run_until_idle())
    run_in_tenant(local.apply("external-upgrade", access(role="developer", system="gitlab")))
    approve(store, "b", "developer", 72)
    asyncio.run(worker.run_until_idle())
    assert store.get("b")["results"][0]["status"] == "manual_review"
    assert store.get("a")["revocations"][0]["status"] == "cancelled"
    assert store.employee("1001", "default")["access_ledger"][0]["status"] == "drifted"
    clock.t += 73 * 3600
    asyncio.run(worker.run_until_idle())
    assert store.employee("1001", "default")["grants"][0]["role"] == "developer"
    assert "grant_drift_detected" in [row["event"] for row in store.audit("a")]


def test_unattempted_manual_grant_rechecks_policy_after_operator_reconcile(db_url):
    from tests.conftest import run_in_tenant
    from tests.test_policy_revalidation import changed
    from tests.test_workflow import ERIN

    clock = Clock()
    store = WorkflowStore(db_url, clock=clock)
    local = LocalDirectoryAdapter(store.engine)
    worker = ExecutionWorker(store, AdapterRegistry(local))
    approve(store, "a", "reporter", 24)
    asyncio.run(worker.run_until_idle())
    run_in_tenant(local.apply("external-upgrade", access(role="developer", system="gitlab")))
    approve(store, "b", "developer", 72)
    asyncio.run(worker.run_until_idle())
    assert store.get("b")["results"][0]["status"] == "manual_review"
    store.policy = changed("deny-developer", lambda policy: policy["systems"]["gitlab"]["roles"].pop("developer"))
    store.requeue("b", ERIN)
    asyncio.run(worker.run_until_idle())
    assert store.get("b")["results"][0]["status"] == "failed"
    assert len(store.employee("1001", "default")["access_ledger"]) == 1


@pytest.mark.parametrize("old_effect_applied", [False, True])
def test_unknown_revoke_replans_after_a_new_grant_without_reusing_an_applied_command(db_url, old_effect_applied):
    from core.tenant_context import system_scope
    from tests.conftest import run_in_tenant

    clock = Clock()
    store = WorkflowStore(db_url, clock=clock)
    local = LocalDirectoryAdapter(store.engine)
    worker = ExecutionWorker(store, AdapterRegistry(local))
    approve(store, "a", "developer", 24)
    asyncio.run(worker.run_until_idle())
    clock.t += 24 * 3600 + 1
    with system_scope():
        revoke = store.claim("interrupted", 60, 1)[0]
    store.save_result(revoke, {"restore_to": None})
    store.retry_later(revoke, "unknown", "interrupted before settlement", 10)
    approve(store, "b", "developer", 72)
    # Historical rows could have been claimed while another command was UNKNOWN.
    from sqlalchemy import update
    from core.db import approvals
    with store.engine.begin() as db:
        row = db.execute(select(actions).where(actions.c.approval_id == "b", actions.c.kind == "apply")).mappings().one()
        db.execute(update(actions).where(actions.c.id == row["id"]).values(status="executing", lease_owner="legacy",
            lease_until=clock.t + 60, attempts=1, version=row["version"] + 1))
        db.execute(update(approvals).where(approvals.c.id == "b").values(execution="executing"))
    legacy = {**row, "status": "executing", "lease_owner": "legacy", "attempts": 1, "version": row["version"] + 1}
    store.save_result(legacy, {"baseline_level": None})
    store.succeed(legacy, {"baseline_level": None, "covered_by": "a"})
    if old_effect_applied:
        run_in_tenant(local.apply(revoke["id"], {"tool_name": "revoke_access", "arguments": {
            "employee_id": "1001", "system": "gitlab", "restore_to": None}}))
    clock.t += 11
    asyncio.run(worker.run_once(limit=1))
    result = store.get("a")["revocations"][0]
    if old_effect_applied:
        assert result["status"] == "manual_review" and "reconciled" not in result
        assert store.employee("1001", "default")["grants"] == []
    else:
        assert result["status"] == "succeeded" and result["restore_to"] == "developer"
        assert store.employee("1001", "default")["grants"][0]["role"] == "developer"


@pytest.mark.parametrize("held_status", ["unknown", "manual_review"])
def test_unsettled_apply_keeps_the_resource_until_its_baseline_is_in_the_ledger(db_url, held_status):
    from core.tenant_context import system_scope
    from tests.conftest import run_in_tenant
    from tests.test_workflow import ERIN

    clock = Clock()
    store = WorkflowStore(db_url, clock=clock)
    local = LocalDirectoryAdapter(store.engine)
    worker = ExecutionWorker(store, AdapterRegistry(local))
    approve(store, "a", "reporter", 24)
    with system_scope():
        first = store.claim("interrupted", 60, 1)[0]
    store.save_result(first, {"baseline_level": None})
    run_in_tenant(local.apply(first["id"], {"tool_name": first["tool_name"], "arguments": first["arguments"]}))
    if held_status == "unknown":
        store.retry_later(first, "unknown", "interrupted after effect", 10)
    else:
        store.fail(first, "effect requires review", status="manual_review")
    approve(store, "b", "developer", 72)
    assert asyncio.run(worker.run_once()) == 0
    assert store.get("b")["results"][0]["status"] == "queued"
    if held_status == "manual_review":
        store.requeue("a", ERIN)
    else:
        clock.t += 11
    asyncio.run(worker.run_until_idle())
    assert all(grant["baseline_role"] is None for grant in store.employee("1001", "default")["access_ledger"])
    clock.t += 24 * 3600 + 1
    asyncio.run(worker.run_until_idle())
    assert store.employee("1001", "default")["grants"][0]["role"] == "developer"
    clock.t += 48 * 3600
    asyncio.run(worker.run_until_idle())
    assert store.employee("1001", "default")["grants"] == []


def test_claim_collision_does_not_block_a_different_resource_and_expired_lease_can_be_reclaimed(db_url):
    clock = Clock()
    store = WorkflowStore(db_url, clock=clock)
    approve(store, "a", "reporter", 24)
    approve(store, "b", "developer", 72)
    other = store.create("c", request_id="c", conv_id="c", principal=ALICE,
                         action_requests=[access(role="reporter", system="jira")])
    store.decide("c", "approved", BOB, other["payload_hash"])
    first = store.claim("one", 60, 1)[0]
    second = store.claim("two", 60, 3)
    assert len(second) == 1 and second[0]["resource_key"] != first["resource_key"]
    clock.t += 61
    reclaimed = store.claim("three", 60, 3)
    assert {row["id"] for row in reclaimed} == {first["id"], second[0]["id"]}


def test_migration_backfills_resource_keys_and_removes_frozen_revoke_baseline(db_url):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import MetaData, Table, insert

    from core.db import make_engine, migrate
    from core.tenant_context import system_scope

    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "migrations"))
    config.set_main_option("sqlalchemy.url", db_url.replace("%", "%%"))
    with system_scope():
        command.upgrade(config, "0005")
        engine = make_engine(db_url)
        table = Table("actions", MetaData(), autoload_with=engine)
        with engine.begin() as db:
            db.execute(insert(table).values(
                id="old-revoke", tenant="default", approval_id="old", seq=1000, kind="revoke",
                tool_name="revoke_access", arguments={"employee_id": "1001", "system": "gitlab", "role": "developer", "baseline_level": None},
                adapter="local", status="queued", attempts=0, not_before=1, version=0, created_at=1, updated_at=1))
        migrate(db_url)
        with engine.connect() as db:
            migrated = db.execute(select(actions)).mappings().one()
        engine.dispose()
    assert migrated["resource_key"] == "default:access:1001:gitlab"
    assert "baseline_level" not in migrated["arguments"]


def test_operator_closes_a_deliberate_drift_and_releases_the_resource(db_url):
    from sqlalchemy import update
    from core.db import directory_grants
    from core.tenant_context import tenant_scope
    from tests.test_workflow import ERIN
    from workflow.store import ApprovalStateConflict

    clock = Clock()
    store = WorkflowStore(db_url, clock=clock)
    worker = ExecutionWorker(store, AdapterRegistry(LocalDirectoryAdapter(store.engine)))
    approve(store, "a", "reporter", 24)
    asyncio.run(worker.run_until_idle())
    # Someone deliberately raised the permission outside the service: observing again never passes.
    with tenant_scope("default"), store.engine.begin() as db:
        db.execute(update(directory_grants).where(directory_grants.c.employee_id == "1001").values(role="admin"))
    clock.t += 24 * 3600 + 1
    asyncio.run(worker.run_until_idle())
    store.requeue("a", ERIN)
    asyncio.run(worker.run_until_idle())
    assert store.get("a")["revocations"][0]["status"] == "manual_review"
    approve(store, "b", "developer", 8)
    asyncio.run(worker.run_until_idle())
    assert store.get("b")["results"][0]["status"] == "queued"

    body = store.resolve_manual("a", ERIN, "管理员权限由平台负责人保留")
    assert body["revocations"][0]["status"] == "cancelled"
    assert [e["event"] for e in store.audit("a")][-1] == "manual_resolved"
    assert store.employee("1001", "default")["access_ledger"][0]["status"] == "drifted"
    with pytest.raises(ApprovalStateConflict):
        store.resolve_manual("a", ERIN, "again")
    asyncio.run(worker.run_until_idle())
    assert store.get("b")["results"][0]["status"] == "succeeded"


def test_closing_after_a_non_reversible_effect_settles_the_execution(db_url):
    from tests.test_workflow import ERIN

    store = WorkflowStore(db_url, clock=Clock())
    body = store.create("t", request_id="r-t", conv_id="conv-t", principal=ALICE, action_requests=[
        {"tool_name": "create_ticket", "arguments": {"employee_id": "1001", "title": "VPN", "description": "691"}},
        access(role="developer", duration_hours=8)])
    for principal in (BOB, CAROL):
        store.decide("t", "approved", principal, body["payload_hash"])

    class Ambiguous(LocalDirectoryAdapter):
        async def apply(self, action_id, action):
            if action["tool_name"] == "request_access":
                from workflow.adapters.base import AmbiguousError
                raise AmbiguousError("timeout")
            return await super().apply(action_id, action)

        async def observe(self, action_id, action):
            return False if action["tool_name"] == "request_access" else await super().observe(action_id, action)

    worker = ExecutionWorker(store, AdapterRegistry(Ambiguous(store.engine)), max_attempts=1)
    asyncio.run(worker.run_until_idle())
    assert store.get("t")["execution"] == "manual_review"
    assert store.resolve_manual("t", ERIN, "权限已在目标系统人工开通")["execution"] == "failed"
    asyncio.run(worker.run_until_idle())
    assert store.get("t")["execution"] == "failed"
