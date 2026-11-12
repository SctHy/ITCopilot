import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import update

from core.db import directory_tickets
from core.tenant_context import tenant_scope
from monitor.performance_monitor import PerformanceMonitor
from tests.conftest import run_in_tenant
from tests.test_workflow import ALICE, BOB, Clock, access, create
from tests.test_shared_tasks import seed_participants
from workflow.adapters import AdapterRegistry
from workflow.adapters.base import AmbiguousError, TransientError
from workflow.adapters.local import LocalDirectoryAdapter
from workflow.goal_verifiers import GoalVerifiers
from workflow.reconciler import GrantReconciler
from workflow.store import WorkflowStore
from workflow.tasks import TaskStore
from workflow.worker import ExecutionWorker


def environment(url):
    clock = Clock()
    store = WorkflowStore(url, clock=clock)
    adapter = LocalDirectoryAdapter(store.engine)
    registry = AdapterRegistry(adapter)
    worker = ExecutionWorker(store, registry)
    return clock, store, adapter, registry, worker


def grant(store, worker, name="a", conv_id="c"):
    body = store.create(name, request_id=name, conv_id=conv_id, principal=ALICE, action_requests=[access(role="reporter", duration_hours=24)])
    store.decide(name, "approved", BOB, body["payload_hash"])
    asyncio.run(worker.run_until_idle())


def test_reconciler_observes_drift_cancels_revoke_and_replays_task_notice_after_restart(db_url):
    clock, store, adapter, registry, worker = environment(db_url)
    tasks = TaskStore(store.engine, clock=clock)
    task = tasks.create(ALICE, "权限申请", "给我 reporter", "task")
    seed_participants(tasks, task["id"])
    store.shared_access = tasks.access.approval_access
    grant(store, worker, conv_id="task:" + task["id"])
    run_in_tenant(adapter.apply("manual", access(role="developer", system="gitlab")))
    assert asyncio.run(GrantReconciler(store, registry).run_once()) == 1
    state = store.employee("1001", "default")
    assert state["grants"][0]["role"] == "developer"
    assert state["access_ledger"][0]["status"] == "drifted"
    assert store.get("a")["revocations"][0]["status"] == "cancelled"
    assert store.get("a")["revocations"][0]["error"] == "巡检发现漂移"
    assert [row["event"] for row in store.audit("a")].count("grant_drift_detected") == 1
    reconciler = GrantReconciler(store, registry, tasks)
    asyncio.run(reconciler.run_once())
    asyncio.run(reconciler.run_once())
    notices = [event for event in tasks.get(task["id"], ALICE)["events"] if event["kind"] == "grant_drift"]
    assert len(notices) == 1


@pytest.mark.parametrize("error", [TransientError, AmbiguousError])
def test_reconciler_skips_temporary_adapter_failure_without_settling_grants(db_url, error):
    _, store, _, _, worker = environment(db_url)
    grant(store, worker)

    class Unavailable(LocalDirectoryAdapter):
        async def current_level(self, employee_id, system):
            raise error("injected failure")

    assert asyncio.run(GrantReconciler(store, AdapterRegistry(Unavailable(store.engine))).run_once()) == 0
    assert store.employee("1001", "default")["access_ledger"][0]["status"] == "active"


def test_reconciler_propagates_unexpected_error_and_does_not_treat_it_as_drift(db_url):
    _, store, _, _, worker = environment(db_url)
    grant(store, worker)

    class Broken(LocalDirectoryAdapter):
        async def current_level(self, employee_id, system):
            raise ValueError("programming error")

    with pytest.raises(ValueError, match="programming error"):
        asyncio.run(GrantReconciler(store, AdapterRegistry(Broken(store.engine))).run_once())


def test_concurrent_reconcilers_record_drift_once(db_url):
    _, store, adapter, registry, worker = environment(db_url)
    grant(store, worker)
    run_in_tenant(adapter.apply("manual", access(role="developer", system="gitlab")))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: asyncio.run(GrantReconciler(store, registry).run_once()), range(2)))
    assert sum(results) == 1
    assert [row["event"] for row in store.audit("a")].count("grant_drift_detected") == 1


def test_worker_runs_reconciliation_at_interval_not_each_poll(db_url):
    clock, store, adapter, _, worker = environment(db_url)
    grant(store, worker)
    run_in_tenant(adapter.apply("manual", access(role="developer", system="gitlab")))
    clock.t += 899
    asyncio.run(worker.run_once())
    assert store.employee("1001", "default")["access_ledger"][0]["status"] == "active"
    clock.t += 1
    asyncio.run(worker.run_once())
    assert store.employee("1001", "default")["access_ledger"][0]["status"] == "drifted"


@pytest.mark.parametrize("value", ["invalid", "nan", "inf", "0", "-1", ""])
def test_invalid_reconcile_interval_refuses_startup(db_url, monkeypatch, value):
    monkeypatch.setenv("IT_COPILOT_RECONCILE_INTERVAL_S", value)
    store = WorkflowStore(db_url)
    with pytest.raises(ValueError):
        ExecutionWorker(store, AdapterRegistry(LocalDirectoryAdapter(store.engine)))


def test_workflow_ages_warn_escalate_and_clear(db_url):
    clock, store, _, _, _ = environment(db_url)
    body = create(store)
    store.decide("a", "approved", BOB, body["payload_hash"])
    monitor = PerformanceMonitor(None, None, workflow=store)
    clock.t += 601
    assert monitor._workflow_conditions()["oldest_queued_due_action_age_s"].severity == "warning"
    claimed = store.claim("worker", 60, 1)[0]
    store.retry_later(claimed, "unknown", "timeout", 0)
    clock.t += 3601
    assert monitor._workflow_conditions()["oldest_unknown_action_age_s"].severity == "warning"
    clock.t += 86400
    assert monitor._workflow_conditions()["oldest_unknown_action_age_s"].severity == "critical"
    claimed = store.claim("worker", 60, 1)[0]
    store.fail(claimed, "target rejected")
    assert monitor._workflow_conditions() == {}


def test_device_and_ticket_verifiers_read_current_owning_employee_state(db_url):
    _, store, adapter, registry, _ = environment(db_url)
    verifier = GoalVerifiers(registry, store.engine)
    task = {"tenant": "default", "principal": {"employee_id": "1001"}}
    checks = [{"kind": "device_locked", "device_id": "DEV-1001"}, {"kind": "ticket_open", "ticket_id": "INC-ticket"}]
    with tenant_scope("default"):
        with store.engine.begin() as db:
            db.execute(directory_tickets.insert().values(tenant="default", id="INC-ticket", employee_id="1001", title="title", description="desc", status="open"))
    observations = asyncio.run(verifier.verify(task, checks))
    assert [row["status"] for row in observations] == ["failed", "passed"]
    run_in_tenant(adapter.apply("lock", {"tool_name": "lock_device", "arguments": {"employee_id": "1001", "device_id": "DEV-1001"}}))
    with tenant_scope("default"):
        with store.engine.begin() as db:
            db.execute(update(directory_tickets).where(directory_tickets.c.tenant == "default", directory_tickets.c.id == "INC-ticket").values(status="closed"))
    observations = asyncio.run(verifier.verify(task, checks))
    assert [row["status"] for row in observations] == ["passed", "failed"]
    unknown_device = asyncio.run(verifier.verify(task, [{"kind": "device_locked", "device_id": "DEV-missing"}]))
    assert unknown_device[0]["status"] == "unknown"
    other = {"tenant": "other", "principal": {"employee_id": "1001"}}
    assert [row["status"] for row in asyncio.run(verifier.verify(other, checks))] == ["unknown", "failed"]


def test_verifiers_report_unknown_when_the_database_is_unreachable(tmp_path):
    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{tmp_path / 'unmounted' / 'directory.sqlite3'}")
    verifier = GoalVerifiers(None, engine)
    task = {"tenant": "default", "principal": {"employee_id": "1001"}}
    checks = [{"kind": "device_locked", "device_id": "DEV-1001"}, {"kind": "ticket_open", "ticket_id": "INC-ticket"}]
    observations = asyncio.run(verifier.verify(task, checks))
    assert [row["status"] for row in observations] == ["unknown", "unknown"]
    engine.dispose()
