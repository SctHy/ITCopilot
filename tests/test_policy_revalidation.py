"""Policy changes re-check open work: pending approvals, queued execution and live grants (ADR 0008)."""
import asyncio
import copy

import pytest
import yaml

from workflow.adapters import AdapterRegistry
from workflow.adapters.local import LocalDirectoryAdapter
from workflow import policy as policy_module
from workflow.policy import AccessPolicy, load_policy
from workflow.store import WorkflowStore
from workflow.worker import ExecutionWorker
from tests.test_workflow import BOB, CAROL, Clock, access, create, run


def changed(version, edit):
    data = copy.deepcopy(load_policy().data)
    data["version"] = version
    edit(data)
    return AccessPolicy(data)


def without_gitlab_readonly(data):
    del data["systems"]["gitlab"]["roles"]["readonly"]


@pytest.fixture
def env(db_url):
    clock = Clock()
    notices = []
    store = WorkflowStore(db_url, clock=clock, policy=load_policy(), notify=lambda db, notice: notices.append(notice))
    worker = ExecutionWorker(store, AdapterRegistry(LocalDirectoryAdapter(store.engine)), worker_id="w1", base_delay_s=0)
    return store, worker, clock, notices


def events(store, approval_id):
    return [e["event"] for e in store.audit(approval_id)]


def test_pending_approval_no_longer_allowed_is_invalidated(env):
    store, worker, _, notices = env
    pending = create(store)
    store.policy = changed("v2", without_gitlab_readonly)
    report = store.revalidate()
    assert report["invalidated"] == ["a"]
    assert store.get("a")["status"] == "invalidated" and "policy_invalidated" in events(store, "a")
    assert notices[-1]["event"] == "invalidated" and "v2" in notices[-1]["detail"]["reason"]
    # An approver acting on the stale card cannot revive it.
    assert store.decide("a", "approved", BOB, pending["payload_hash"])["status"] == "invalidated"
    run(worker)
    assert store.employee("1001")["grants"] == []
    assert store.revalidate()["invalidated"] == []  # idempotent


def test_decision_rechecks_without_a_sweep(env):
    store, _, _, _ = env
    pending = create(store)
    store.policy = changed("v2", without_gitlab_readonly)
    assert store.decide("a", "approved", BOB, pending["payload_hash"])["status"] == "invalidated"


def test_stricter_quorum_applies_to_pending_but_looser_never_lowers_it(env):
    store, worker, _, _ = env
    pending = create(store)
    store.policy = changed("v2", lambda d: d["systems"]["gitlab"]["roles"]["readonly"].update(approvals=2))
    assert store.revalidate()["quorum_raised"] == ["a"]
    assert store.decide("a", "approved", BOB, pending["payload_hash"])["status"] == "pending"
    assert store.decide("a", "approved", CAROL, pending["payload_hash"])["status"] == "approved"
    run(worker)
    assert store.get("a")["execution"] == "succeeded"

    dev = create(store, "b", actions=[access(role="developer", system="jira")])
    assert dev["approvals_required"] == 2
    store.policy = changed("v3", lambda d: d["systems"]["jira"]["roles"]["developer"].update(approvals=1))
    store.revalidate()
    assert store.decide("b", "approved", BOB, dev["payload_hash"])["status"] == "pending"


def test_approved_but_unexecuted_work_is_cancelled_before_any_effect(env):
    store, worker, _, notices = env
    pending = create(store)
    store.decide("a", "approved", BOB, pending["payload_hash"])
    store.policy = changed("v2", without_gitlab_readonly)
    assert store.revalidate()["cancelled"] == ["a"]
    body = store.get("a")
    assert body["execution"] == "failed" and body["results"][0]["status"] == "cancelled"
    assert notices[-1]["event"] == "execution_failed" and "v2" in notices[-1]["detail"]["error"]
    run(worker)
    assert store.employee("1001")["grants"] == []


def test_worker_refuses_first_effect_when_policy_changed_after_approval(env):
    store, worker, _, _ = env
    pending = create(store)
    store.decide("a", "approved", BOB, pending["payload_hash"])
    store.policy = changed("v2", without_gitlab_readonly)
    worker._policy_version = "v2"  # the sweep has not run yet: only the pre-effect check stands in the way
    run(worker)
    body = store.get("a")
    assert body["execution"] == "failed" and "v2" in body["results"][0]["error"]
    assert store.employee("1001")["grants"] == []


def test_live_grants_are_shortened_or_revoked_early(env):
    store, worker, clock, _ = env
    for approval_id, system in (("a", "gitlab"), ("b", "jira")):
        body = create(store, approval_id, actions=[access(system=system, duration_hours=100)])
        store.decide(approval_id, "approved", BOB, body["payload_hash"])
    run(worker)
    assert {g["system"] for g in store.employee("1001")["grants"]} == {"gitlab", "jira"}

    def edit(data):
        without_gitlab_readonly(data)
        data["systems"]["jira"]["roles"]["readonly"]["max_hours"] = 10

    store.policy = changed("v2", edit)
    report = store.revalidate()
    assert len(report["grants_revoked"]) == 1 and len(report["grants_shortened"]) == 1
    run(worker)
    assert [g["system"] for g in store.employee("1001")["grants"]] == ["jira"]  # gitlab revoked now
    clock.t += 10 * 3600 + 1
    run(worker)
    assert store.employee("1001")["grants"] == []
    assert "policy_shortened" in events(store, "b") and "policy_revoked" in events(store, "a")
    assert store.revalidate()["grants_shortened"] == []


def test_policy_file_edits_apply_without_restart_and_a_broken_edit_keeps_the_last_good(tmp_path, monkeypatch):
    path = tmp_path / "policy.yaml"
    data = copy.deepcopy(load_policy().data)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setenv("IT_COPILOT_ACCESS_POLICY", str(path))
    assert load_policy().version == data["version"]
    data["version"] = "edited"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert load_policy().version == "edited"
    path.write_text("version: [unclosed", encoding="utf-8")
    assert load_policy().version == "edited"
    policy_module._last_good.pop(str(path))


def test_worker_sweeps_once_per_version(env):
    store, worker, _, _ = env
    create(store)
    asyncio.run(worker.run_once())
    store.policy = changed("v2", without_gitlab_readonly)
    asyncio.run(worker.run_once())
    assert store.get("a")["status"] == "invalidated" and worker._policy_version == "v2"


def test_policy_endpoints(env, monkeypatch):
    from fastapi.testclient import TestClient

    import api.main as api
    from api import state
    from tests.conftest import auth

    store, *_ = env
    monkeypatch.setattr(state, "workflow", store)
    monkeypatch.setattr(state, "worker", None)
    http = TestClient(api.app)
    assert http.get("/policy", headers=auth("alice")).json()["version"] == store.policy.version
    assert http.post("/policy/revalidate", headers=auth("alice")).status_code == 403
    create(store)
    store.policy = changed("v2", without_gitlab_readonly)
    assert http.post("/policy/revalidate", headers=auth("erin")).json()["invalidated"] == ["a"]
    assert [a["approval_id"] for a in http.get("/approvals?status=invalidated", headers=auth("alice")).json()["items"]] == ["a"]
