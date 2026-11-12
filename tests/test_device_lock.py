"""Remote lock of a lost device: proposed by the agent, approved by someone else, executed by the worker
against the sandbox inventory, and only ever for a device the named employee holds."""
from tests.conftest import run_in_tenant

import pytest

from agents.tools import lock_device
from agents.types import Request
from intake.im import _results_text
from tests.test_workflow import ALICE, BOB, ERIN, env, run  # noqa: F401 (env is a fixture)
from workflow.approval import approval_from_traces
from workflow.policy import PolicyViolation
from workflow.store import ApprovalForbidden


def lock(device="DEV-1001", employee="1001"):
    return {"tool_name": "lock_device", "arguments": {"employee_id": employee, "device_id": device, "reason": "笔记本在地铁上丢失"}}


def devices(store, employee="1001"):
    return {d["device_id"]: d["locked"] for d in store.employee(employee, "default")["devices"]}


def test_tool_only_proposes_and_only_for_the_callers_own_account():
    req = Request("我的电脑 dev-1001 丢了，帮我锁定", principal=ALICE)
    pending = lock_device(req, dict(lock("dev-1001")["arguments"]))
    # Locking a device is in the policy's requester_confirmation list: the requester checks the device first.
    assert pending["status"] == "awaiting_confirmation" and pending["requires_requester_confirmation"]
    assert pending["request"]["device_id"] == "DEV-1001"
    with pytest.raises(PolicyViolation):
        lock_device(Request("锁定 dave 的电脑", principal=ALICE), dict(lock("DEV-1002", "1002")["arguments"]))
    assert lock_device(Request("x", principal=ERIN), dict(lock("DEV-1002", "1002")["arguments"]))["status"] == "awaiting_confirmation"  # IT on behalf
    traces = [{"tool_name": "lock_device", "input": lock()["arguments"], "success": True}]
    approval = approval_from_traces(traces, "电脑丢了")
    assert approval["actions"] == ["lock_device"] and approval["risk"] == "high"


def test_approved_lock_is_applied_once_and_reported(env):  # noqa: F811
    store, worker, _ = env
    assert devices(store) == {"DEV-1001": False}
    pending = store.create("a", request_id="r", conv_id="c", principal=ALICE, action_requests=[lock("dev-1001")])
    with pytest.raises(ApprovalForbidden):
        store.decide("a", "approved", ALICE, pending["payload_hash"])      # nobody approves their own request
    store.decide("a", "approved", BOB, pending["payload_hash"])
    run(worker)
    body = store.get("a")
    assert body["execution"] == "succeeded" and body["results"][0]["device_id"] == "DEV-1001"
    assert devices(store) == {"DEV-1001": True} and devices(store, "1002") == {"DEV-1002": False}
    assert _results_text(body["results"]) == "：设备 DEV-1001 已远程锁定"
    # Re-applying the same action id returns the recorded effect instead of repeating it.
    adapter = worker.registry.local
    again = run_in_tenant(adapter.apply(body["results"][0]["operation_id"], lock()))
    assert again == {"action": "lock_device", "employee_id": "1001", "device_id": "DEV-1001"}


def test_a_device_held_by_someone_else_is_never_locked(env):  # noqa: F811
    store, worker, _ = env
    # IT files it on alice's account but with dave's asset tag: the adapter refuses before any effect.
    pending = store.create("a", request_id="r", conv_id="c", principal=ERIN, action_requests=[lock("DEV-1002", "1001")])
    store.decide("a", "approved", BOB, pending["payload_hash"])
    run(worker)
    body = store.get("a")
    assert body["execution"] == "failed" and "不属于" in body["results"][0]["error"]
    assert devices(store, "1002") == {"DEV-1002": False}
    unknown = store.create("b", request_id="r2", conv_id="c2", principal=ALICE, action_requests=[lock("DEV-9999")])
    store.decide("b", "approved", BOB, unknown["payload_hash"])
    run(worker)
    assert store.get("b")["execution"] == "failed"
