"""Durable approval workflow: CAS decisions, SoD, quorum, outbox worker, reconciliation, expiry, saga.

Runs on SQLite by default. Set IT_COPILOT_TEST_DATABASE_URL=postgresql+psycopg://... to run the
same tests on PostgreSQL (each test gets its own schema).
"""
import asyncio
import os
import uuid
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from adapters import AdapterRegistry
from adapters.base import AmbiguousError
from adapters.local import LocalDirectoryAdapter
from core.identity import Principal
from workflow.policy import PolicyViolation
from workflow.store import ApprovalConflict, ApprovalForbidden, WorkflowStore
from workflow.worker import ExecutionWorker

ALICE = Principal("dev:alice", "alice", "engineering", frozenset({"employee"}), "1001")
BOB = Principal("dev:bob", "bob", "engineering", frozenset({"employee", "approver"}), "2001")
CAROL = Principal("dev:carol", "carol", "engineering", frozenset({"employee", "approver"}), "2002")
ERIN = Principal("dev:erin", "erin", "it", frozenset({"employee", "it_admin"}), "9001")


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


def access(employee="1001", role="readonly", system="GitLab", **extra):
    return {"tool_name": "request_access", "arguments": {"employee_id": employee, "system": system, "role": role, "reason": "代码审阅", **extra}}


@pytest.fixture
def db_url(tmp_path):
    base = os.getenv("IT_COPILOT_TEST_DATABASE_URL")
    if not base:
        yield f"sqlite:///{tmp_path / 'wf.sqlite3'}"
        return
    from sqlalchemy import create_engine, text
    schema = "t_" + uuid.uuid4().hex[:12]
    admin = create_engine(base)
    with admin.begin() as db:
        db.execute(text(f"CREATE SCHEMA {schema}"))
    yield f"{base}{'&' if '?' in base else '?'}options=-csearch_path%3D{schema}"
    with admin.begin() as db:
        db.execute(text(f"DROP SCHEMA {schema} CASCADE"))
    admin.dispose()


@pytest.fixture
def env(db_url):
    clock = Clock()
    store = WorkflowStore(db_url, clock=clock)
    worker = ExecutionWorker(store, AdapterRegistry(LocalDirectoryAdapter(store.engine)), worker_id="w1", base_delay_s=0)
    return store, worker, clock


def create(store, approval_id="a", principal=ALICE, actions=None):
    return store.create(approval_id, request_id="r-" + approval_id, conv_id="c", principal=principal, action_requests=actions or [access()])


def run(worker):
    asyncio.run(worker.run_until_idle())


def test_frozen_hash_and_concurrent_approvals_execute_once(env):
    store, worker, _ = env
    pending = create(store)
    assert pending["status"] == "pending" and pending["policy_version"] and pending["approvals_required"] == 1
    assert create(store) == pending  # idempotent re-create
    with pytest.raises(ApprovalConflict):
        create(store, actions=[access(role="reporter")])
    with pytest.raises(ApprovalConflict, match="哈希"):
        store.decide("a", "approved", BOB, "0" * 64)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: store.decide("a", "approved", BOB, pending["payload_hash"]), range(20)))
    assert store.get("a")["execution"] == "queued"
    assert store.employee("1001")["grants"] == []  # nothing happens before the worker
    run(worker)
    body = store.get("a")
    assert body["execution"] == "succeeded" and len(body["results"]) == 1
    assert store.employee("1001")["grants"] == [{"system": "gitlab", "role": "readonly", "operation_id": body["results"][0]["operation_id"]}]
    events = [e["event"] for e in store.audit("a")]
    assert events.count("approved") == 1 and events.count("apply_succeeded") == 1


def test_competing_approve_and_reject_are_atomic(env):
    store, worker, _ = env
    pending = create(store)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda p: store.decide("a", p[0], p[1], pending["payload_hash"]), [("approved", BOB), ("rejected", CAROL)]))
    run(worker)
    body = store.get("a")
    assert body["status"] in {"approved", "rejected"} and len(body["decisions"]) == 1
    assert bool(store.employee("1001")["grants"]) == (body["status"] == "approved")


def test_separation_of_duties_and_authority(env):
    store, _, _ = env
    pending = create(store)
    h = pending["payload_hash"]
    with pytest.raises(ApprovalForbidden, match="没有审批权限"):
        store.decide("a", "approved", Principal("dev:dave", "dave", "engineering", frozenset({"employee"}), "1002"), h)
    # An approver requesting for themselves cannot approve it either.
    own = create(store, "b", BOB, [access("2001")])
    with pytest.raises(ApprovalForbidden, match="自己发起"):
        store.decide("b", "approved", BOB, own["payload_hash"])
    # An admin files for bob; bob still cannot approve a change to his own account.
    for_bob = create(store, "c", ERIN, [access("2001")])
    with pytest.raises(ApprovalForbidden, match="自己账号"):
        store.decide("c", "approved", BOB, for_bob["payload_hash"])
    assert store.decide("c", "approved", CAROL, for_bob["payload_hash"])["status"] == "approved"


def test_policy_blocks_admin_roles_long_grants_and_requests_for_others(env):
    store, _, _ = env
    with pytest.raises(PolicyViolation, match="管理员权限"):
        create(store, actions=[access(role="maintainer")])
    with pytest.raises(PolicyViolation, match="最长"):
        create(store, actions=[access(role="developer", duration_hours=500)])
    with pytest.raises(PolicyViolation, match="本人"):
        create(store, actions=[access("1002")])
    assert create(store, "d", ERIN, [access("1002")])["status"] == "pending"


def test_developer_access_needs_two_distinct_approvers_even_concurrently(env):
    store, worker, _ = env
    pending = create(store, actions=[access(role="developer")])
    assert pending["approvals_required"] == 2
    h = pending["payload_hash"]
    assert store.decide("a", "approved", BOB, h)["status"] == "pending"
    assert store.decide("a", "approved", BOB, h)["status"] == "pending"  # same approver twice is one vote
    assert store.decide("a", "approved", CAROL, h)["status"] == "approved"
    # Concurrent votes from two approvers still reach the quorum exactly once.
    other = create(store, "b", ALICE, [access(role="developer", system="Jira")])
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda p: store.decide("b", "approved", p, other["payload_hash"]), [BOB, CAROL]))
    assert store.get("b")["status"] == "approved"
    run(worker)
    assert {g["system"] for g in store.employee("1001")["grants"]} == {"gitlab", "jira"}


def test_preflight_failure_applies_nothing_and_rejection_never_executes(env):
    store, worker, _ = env
    batch = create(store, "a", ERIN, [access("1001"), access("missing")])
    store.decide("a", "approved", BOB, batch["payload_hash"])
    run(worker)
    body = store.get("a")
    assert body["execution"] == "failed" and [r["status"] for r in body["results"]] == ["failed", "cancelled"]
    assert store.employee("1001")["grants"] == []
    rejected = create(store, "b")
    store.decide("b", "rejected", BOB, rejected["payload_hash"])
    run(worker)
    assert store.get("b")["execution"] == "not_executed" and store.employee("1001")["grants"] == []


def test_grant_expires_and_is_revoked(env):
    store, worker, clock = env
    pending = create(store, actions=[access(duration_hours=2)])
    store.decide("a", "approved", BOB, pending["payload_hash"])
    run(worker)
    assert store.employee("1001")["access_ledger"][0]["status"] == "active"
    clock.t += 3600
    run(worker)
    assert store.employee("1001")["grants"]  # not yet expired
    clock.t += 3601
    run(worker)
    state = store.employee("1001")
    assert state["grants"] == [] and state["access_ledger"][0]["status"] == "revoked"
    assert store.get("a")["revocations"][0]["status"] == "succeeded"


def test_revocation_never_removes_preexisting_or_drifted_access(env):
    store, worker, clock = env
    adapter = worker.registry.local
    asyncio.run(adapter.apply("seed", access(role="developer", system="gitlab")))  # alice already has developer
    pending = create(store, actions=[access(role="readonly", duration_hours=1)])
    store.decide("a", "approved", BOB, pending["payload_hash"])
    run(worker)
    assert store.get("a")["revocations"] == []
    assert store.employee("1001")["access_ledger"][0]["status"] == "preexisting"
    # A grant whose permission someone changes afterwards is left for review, not deleted.
    other = create(store, "b", ALICE, [access(system="Jira", duration_hours=1)])
    store.decide("b", "approved", BOB, other["payload_hash"])
    run(worker)
    asyncio.run(adapter.apply("manual-upgrade", access(system="jira", role="reporter")))
    clock.t += 7200
    run(worker)
    roles = {g["system"]: g["role"] for g in store.employee("1001")["grants"]}
    assert roles == {"gitlab": "developer", "jira": "reporter"}
    assert store.get("b")["revocations"][0]["status"] == "manual_review"
    assert {g["system"]: g["status"] for g in store.employee("1001")["access_ledger"]}["jira"] == "drifted"


class FlakyAdapter(LocalDirectoryAdapter):
    """Raises an ambiguous error after (or instead of) applying, like a timeout on the response."""

    def __init__(self, engine, applied_before_error):
        super().__init__(engine)
        self.applied_before_error = applied_before_error
        self.calls = 0

    async def apply(self, action_id, action):
        self.calls += 1
        if self.calls == 1:
            if self.applied_before_error:
                await super().apply(action_id, action)
            raise AmbiguousError("timeout after send")
        return await super().apply(action_id, action)


@pytest.mark.parametrize("applied_before_error,expected_calls", [(True, 1), (False, 2)])
def test_ambiguous_outcome_is_reconciled_before_retry(env, applied_before_error, expected_calls):
    store, _, _ = env
    adapter = FlakyAdapter(store.engine, applied_before_error)
    worker = ExecutionWorker(store, AdapterRegistry(adapter), worker_id="w1", base_delay_s=0)
    pending = create(store, actions=[{"tool_name": "reset_password", "arguments": {"employee_id": "1001"}}])
    store.decide("a", "approved", BOB, pending["payload_hash"])
    asyncio.run(worker.run_once())
    assert store.get("a")["results"][0]["status"] == "unknown"
    run(worker)
    body = store.get("a")
    assert body["execution"] == "succeeded" and body["results"][0].get("reconciled", False) == applied_before_error
    assert adapter.calls == expected_calls
    assert store.employee("1001")["password_version"] == 1


def test_worker_crash_after_effect_is_recovered_without_repeating(db_url):
    url = db_url
    store = WorkflowStore(url)
    pending = create(store, actions=[{"tool_name": "reset_password", "arguments": {"employee_id": "1001"}}])
    store.decide("a", "approved", BOB, pending["payload_hash"])
    script = """
import asyncio, os, sys
from adapters import AdapterRegistry
from adapters.local import LocalDirectoryAdapter
from workflow.store import WorkflowStore
from workflow.worker import ExecutionWorker
store = WorkflowStore(sys.argv[1])
adapter = LocalDirectoryAdapter(store.engine)
original = adapter.apply
async def crash(*args):
    await original(*args)
    os._exit(71)
adapter.apply = crash
asyncio.run(ExecutionWorker(store, AdapterRegistry(adapter), worker_id="doomed", lease_s=0).run_once())
"""
    assert subprocess.run([sys.executable, "-c", script, url], check=False).returncode == 71
    assert store.get("a")["results"][0]["status"] == "executing"  # lease held by a dead worker
    recovered = WorkflowStore(url)
    run(ExecutionWorker(recovered, AdapterRegistry(LocalDirectoryAdapter(recovered.engine)), worker_id="w2"))
    body = recovered.get("a")
    assert body["execution"] == "succeeded" and body["results"][0]["reconciled"] is True
    assert recovered.employee("1001")["password_version"] == 1


def test_permanent_failure_mid_batch_compensates_earlier_grants(env):
    store, worker, _ = env
    adapter = worker.registry.local
    batch = create(store, "a", ERIN, [access("1001"), {"tool_name": "unlock_account", "arguments": {"employee_id": "1002"}}])
    store.decide("a", "approved", BOB, batch["payload_hash"])
    asyncio.run(worker.run_once(limit=1))  # first action (and batch preflight) succeeds
    with store.engine.begin() as db:  # employee deactivated between the two steps
        db.exec_driver_sql("UPDATE directory_employees SET active=0 WHERE id='1002'")
    run(worker)
    body = store.get("a")
    assert [r["status"] for r in body["results"]] == ["succeeded", "failed"]
    assert body["execution"] == "failed" and body["revocations"][0]["status"] == "succeeded"
    assert asyncio.run(adapter.current_level("1001", "gitlab")) is None
    assert "compensation_scheduled" in [e["event"] for e in store.audit("a")]


def test_amended_request_supersedes_the_pending_duplicate_in_the_same_conversation(env):
    store, worker, _ = env
    ticket = lambda title: {"tool_name": "create_ticket", "arguments": {"employee_id": "1001", "title": title, "description": "外接显示器无信号", "priority": "P3"}}  # noqa: E731
    first = create(store, "t1", actions=[ticket("显示器无信号")])
    jira = create(store, "j1", actions=[access(system="Jira")])
    second = create(store, "t2", actions=[ticket("显示器无信号（DEV-998，已换线）")])
    assert second["superseded"] == ["t1"] and jira["superseded"] == []
    assert store.get("t1")["status"] == "superseded" and store.get("j1")["status"] == "pending"
    assert store.decide("t1", "approved", BOB, first["payload_hash"])["status"] == "superseded"  # cannot be revived
    # Different system or different conversation: separate requests, nothing replaced.
    assert create(store, "g1", actions=[access()])["superseded"] == []
    assert store.create("t3", request_id="r-t3", conv_id="other", principal=ALICE, action_requests=[ticket("另一件事")])["superseded"] == []
    # A narrower request never drops a broader pending one.
    both = create(store, "b1", actions=[ticket("合并"), access(system="VPN")])
    assert set(both["superseded"]) == {"t2"} and create(store, "t4", actions=[ticket("只建单")])["superseded"] == []
    for approval_id in ("b1", "t4", "j1", "g1", "t3"):
        store.decide(approval_id, "approved", BOB, store.get(approval_id)["payload_hash"])
    run(worker)
    assert len(store.employee("1001")["tickets"]) == 3  # b1, t4, t3 — t1/t2 never executed
    assert [e["event"] for e in store.audit("t1")][-1] == "superseded"
