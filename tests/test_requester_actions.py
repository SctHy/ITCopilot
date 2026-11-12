"""Requester-side controls: withdraw a pending request, give back time-limited access early.

Both reuse the approval CAS and the revoke action queued at grant time, so they inherit the
existing guarantees (one terminal transition, drift-checked revocation, audit per change).
"""
import dataclasses
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

import api.main as api
from api import state
from tests.conftest import auth, run_in_tenant
from tests.test_workflow import ALICE, BOB, ERIN, Clock, access, create, run
from workflow.adapters import AdapterRegistry
from workflow.adapters.local import LocalDirectoryAdapter
from workflow.worker import ExecutionWorker
from workflow.states import ApprovalStatus
from workflow.store import ApprovalForbidden, ApprovalStateConflict, WorkflowStore
from workflow.task_approvals import STATUS as TASK_STATUS_LABELS

DAVE = dataclasses.replace(ALICE, subject="dev:dave", username="dave", employee_id="1002")


@pytest.fixture
def env(db_url):
    clock = Clock()
    store = WorkflowStore(db_url, clock=clock)
    worker = ExecutionWorker(store, AdapterRegistry(LocalDirectoryAdapter(store.engine)), worker_id="w1", base_delay_s=0)
    return store, worker, clock


# ── withdraw ─────────────────────────────────────────────────────────────────

def test_requester_withdraws_a_pending_request_and_nothing_executes(env):
    store, worker, _ = env
    pending = create(store)
    with pytest.raises(ApprovalForbidden):
        store.withdraw("a", BOB)                                   # an approver cannot withdraw for the requester
    with pytest.raises(ApprovalForbidden):
        store.withdraw("a", dataclasses.replace(ALICE, tenant="other"))  # same subject, other tenant
    body = store.withdraw("a", ALICE, "  不需要了  ")
    assert body["status"] == "withdrawn" and body["execution"] == "not_executed"
    assert store.withdraw("a", ALICE)["status"] == "withdrawn"     # idempotent
    # A vote arriving after the withdrawal changes nothing.
    assert store.decide("a", "approved", BOB, pending["payload_hash"])["status"] == "withdrawn"
    run(worker)
    assert store.employee("1001", "default")["grants"] == [] and store.get("a")["results"] == []
    withdrawn = [e for e in store.audit("a") if e["event"] == "withdrawn"]
    assert withdrawn == [{"event": "withdrawn", "actor": "dev:alice", "action_id": None, "detail": {"reason": "不需要了"}, "at": withdrawn[0]["at"]}]
    assert store.withdraw("missing", ALICE) is None


def test_settled_requests_cannot_be_withdrawn(env):
    store, worker, clock = env
    pending = create(store)
    store.decide("a", "approved", BOB, pending["payload_hash"])
    with pytest.raises(ApprovalStateConflict, match="approved"):
        store.withdraw("a", ALICE)
    late = create(store, "b", ALICE, [access(system="Jira")])
    clock.t = 1_800_000_000.0 + store.policy.approval_ttl_hours * 3600
    # Past the deadline the request is expired, not withdrawn: the record says what really happened.
    assert store.withdraw("b", ALICE)["status"] == "expired"
    assert late["status"] == "pending"


def test_withdraw_racing_an_approval_settles_exactly_once(env):
    store, worker, _ = env
    for i in range(10):
        approval_id = f"race-{i}"
        # Own conversation each: a new request in the same conversation would supersede the previous one.
        pending = store.create(approval_id, request_id="r-" + approval_id, conv_id=approval_id, principal=ALICE, action_requests=[access()])

        def attempt(who):
            try:
                return (store.withdraw(approval_id, ALICE) if who == "alice"
                        else store.decide(approval_id, "approved", BOB, pending["payload_hash"]))["status"]
            except ApprovalStateConflict:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(attempt, ["alice", "bob"]))
        body = store.get(approval_id)
        assert body["status"] in {"withdrawn", "approved"}
        events = [e["event"] for e in store.audit(approval_id)]
        assert events.count("withdrawn") + events.count("approved") == 1
        # Actions exist exactly when the approval won.
        assert bool(store.apply_actions(approval_id)) == (body["status"] == "approved")


def test_every_approval_status_has_a_shared_task_label():
    # The shared-task event source formats every status it sees; a missing label would crash its poll.
    assert set(ApprovalStatus) <= set(TASK_STATUS_LABELS)


# ── early release ────────────────────────────────────────────────────────────

def granted(store, worker, approval_id="a", principal=ALICE, actions=None):
    pending = create(store, approval_id, principal, actions or [access(duration_hours=8)])
    store.decide(approval_id, "approved", BOB, pending["payload_hash"])
    run(worker)
    return store.get(approval_id)


def test_requester_gives_access_back_before_it_expires(env):
    store, worker, clock = env
    body = granted(store, worker)
    assert store.employee("1001", "default")["grants"] and body["lifecycle"] == "active_until_revocation"
    with pytest.raises(ApprovalForbidden):
        store.release("a", DAVE)                                    # unrelated colleague
    released = store.release("a", ALICE)
    grant_id = body["results"][0]["operation_id"]
    assert released["released"] == [grant_id]
    run(worker)
    state_ = store.employee("1001", "default")
    assert state_["grants"] == [] and state_["access_ledger"][0]["status"] == "revoked"
    assert store.get("a")["lifecycle"] == "completed"
    assert store.release("a", ALICE)["released"] == []             # idempotent once given back
    audit = [e for e in store.audit("a") if e["event"] == "grant_released"]
    assert len(audit) == 1 and audit[0]["actor"] == "dev:alice" and audit[0]["action_id"] == grant_id
    # Nothing else is scheduled later for the same grant.
    clock.t += 9 * 3600
    run(worker)
    assert [r["status"] for r in store.get("a")["revocations"]] == ["succeeded"]


def test_holder_or_admin_may_release_but_other_tenants_may_not(env):
    store, worker, _ = env
    granted(store, worker, "a", ALICE)
    with pytest.raises(ApprovalForbidden):
        store.release("a", dataclasses.replace(ERIN, tenant="other"))
    assert store.release("a", ERIN)["released"]                     # IT admin, same tenant
    run(worker)
    assert store.employee("1001", "default")["grants"] == []


def test_release_keeps_preexisting_and_drifted_access(env):
    store, worker, _ = env
    adapter = worker.registry.local
    run_in_tenant(adapter.apply("seed", access(role="developer", system="gitlab")))
    granted(store, worker, "pre", ALICE, [access(role="readonly", duration_hours=8)])
    with pytest.raises(ApprovalStateConflict):
        store.release("pre", ALICE)                                 # nothing was granted, nothing to give back
    granted(store, worker, "b", ALICE, [access(system="Jira", duration_hours=8)])
    run_in_tenant(adapter.apply("manual-upgrade", access(system="jira", role="reporter")))
    store.release("b", ALICE)
    run(worker)
    # Early release goes through the same drift check as expiry: someone else's change survives.
    grants = {g["system"]: g["role"] for g in store.employee("1001", "default")["grants"]}
    assert grants == {"gitlab": "developer", "jira": "reporter"}
    assert {g["system"]: g["status"] for g in store.employee("1001", "default")["access_ledger"]}["jira"] == "drifted"


def test_pending_request_has_nothing_to_release(env):
    store, _, _ = env
    create(store)
    with pytest.raises(ApprovalStateConflict):
        store.release("a", ALICE)


# ── HTTP ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def http(env, monkeypatch):
    store, worker, _ = env
    monkeypatch.setattr(state, "workflow", store)
    monkeypatch.setattr(state, "worker", None)   # driven explicitly below
    monkeypatch.setattr(state, "task_event_pump", None)
    return TestClient(api.app), store, worker


def test_withdraw_and_release_endpoints(http):
    client, store, worker = http
    create(store, "w", ALICE, [access(system="Jira")])
    assert client.post("/approvals/w/withdraw", headers=auth("bob")).status_code == 403
    response = client.post("/approvals/w/withdraw", json={"reason": "填错了"}, headers=auth("alice"))
    assert response.status_code == 200 and response.json()["status"] == "withdrawn"
    assert client.post("/approvals/w/withdraw", headers=auth("alice")).json()["status"] == "withdrawn"
    assert [a["approval_id"] for a in client.get("/approvals?status=withdrawn", headers=auth("alice")).json()["items"]] == ["w"]
    assert client.post("/approvals/missing/withdraw", headers=auth("alice")).status_code == 404

    granted(store, worker, "g", ALICE)
    assert client.post("/approvals/g/withdraw", headers=auth("alice")).status_code == 409
    assert client.post("/approvals/g/release", headers=auth("dave")).status_code == 403
    response = client.post("/approvals/g/release", headers=auth("alice"))
    body = response.json()
    assert response.status_code == 200 and len(body["released"]) == 1
    assert body["revocations"][0]["status"] == "queued" and body["revocations"][0]["phase"] == "ready"
    run(worker)
    assert store.employee("1001", "default")["grants"] == []
    assert client.post("/approvals/w/release", headers=auth("alice")).status_code == 409
