import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.conftest import run_in_tenant
from tests.test_workflow import ALICE, BOB, Clock, access
from servicedesk_agent.workflow.adapters import AdapterRegistry
from servicedesk_agent.workflow.adapters.base import AmbiguousError, TransientError
from servicedesk_agent.workflow.adapters.local import LocalDirectoryAdapter
from servicedesk_agent.workflow.reconciler import GrantReconciler
from servicedesk_agent.workflow.store import WorkflowStore
from servicedesk_agent.workflow.worker import ExecutionWorker


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
    grant(store, worker)
    run_in_tenant(adapter.apply("manual", access(role="developer", system="gitlab")))
    assert asyncio.run(GrantReconciler(store, registry).run_once()) == 1
    state = store.employee("1001", "default")
    assert state["grants"][0]["role"] == "developer"
    assert state["access_ledger"][0]["status"] == "drifted"
    assert store.get("a")["revocations"][0]["status"] == "cancelled"
    assert store.get("a")["revocations"][0]["error"] == "巡检发现漂移"
    assert [row["event"] for row in store.audit("a")].count("grant_drift_detected") == 1
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
    monkeypatch.setenv("SERVICEDESK_AGENT_RECONCILE_INTERVAL_S", value)
    store = WorkflowStore(db_url)
    with pytest.raises(ValueError):
        ExecutionWorker(store, AdapterRegistry(LocalDirectoryAdapter(store.engine)))
