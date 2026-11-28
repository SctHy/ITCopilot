import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select, update

from servicedesk_agent.agent.tools import agent_tools
from servicedesk_agent.infra import db as schema
from servicedesk_agent.workflow import devsuite
from servicedesk_agent.workflow.approval import approval_from_traces
from servicedesk_agent.workflow.adapters import AdapterRegistry
from servicedesk_agent.workflow.adapters.base import AmbiguousError, TransientError
from servicedesk_agent.workflow.adapters.local import LocalDirectoryAdapter
from servicedesk_agent.workflow.policy import AccessPolicy, PolicyViolation
from servicedesk_agent.workflow.store import ApprovalConflict, WorkflowStore
from servicedesk_agent.workflow.worker import ExecutionWorker
from tests.test_workflow import ALICE, BOB, CAROL, Clock, access


@pytest.fixture
def store(db_url):
    store = WorkflowStore(db_url, clock=Clock())
    with store.engine.begin() as db:
        db.execute(update(schema.directory_employees).where(schema.directory_employees.c.tenant == "default").values(locked=0))
    yield store
    asyncio.run(store.aclose())


def scenario(store, *, seats=None, employee=ALICE, member=None, expired=False, active=True, outage=False):
    with store.engine.begin() as db:
        if seats is not None:
            db.execute(update(devsuite.inventory).where(devsuite.key(devsuite.inventory, "default"))
                       .values(available_seats=seats, total_seats=seats))
        if member is False:
            db.execute(delete(devsuite.groups).where(devsuite.key(devsuite.groups, "default"),
                       devsuite.groups.c.employee_id == employee.employee_id))
        if expired:
            db.execute(update(devsuite.licenses).where(devsuite.key(devsuite.licenses, "default"),
                       devsuite.licenses.c.employee_id == employee.employee_id).values(expires_at=store.clock()-1))
        db.execute(update(devsuite.employees).where(devsuite.employees.c.tenant == "default",
                   devsuite.employees.c.id == employee.employee_id).values(active=int(active)))
        if outage:
            db.execute(update(devsuite.health).where(devsuite.key(devsuite.health, "default")).values(status="down"))


def approve(store, name, employee=ALICE, request=None):
    body = store.create(name, request_id=name, conv_id=name, principal=employee, ask_requester=True,
                        action_requests=[request or access(employee.employee_id, "standard", "DevSuite")])
    assert body["status"] == "draft"
    store.confirm(name, employee, body["payload_hash"])
    store.decide(name, "approved", CAROL, body["payload_hash"])
    return body


def worker(store, adapter=None):
    return ExecutionWorker(store, AdapterRegistry(adapter or LocalDirectoryAdapter(store.directory)), base_delay_s=0)


def effects(store):
    with store.engine.connect() as db:
        return [r["action"] for r in db.scalars(select(devsuite.effects.c.result))]


def read(store, name, principal=ALICE):
    return agent_tools(lambda: store)[name].handler(SimpleNamespace(principal=principal), {"employee_id": BOB.employee_id})


def test_two_steps_last_seat_and_waiting(store):
    scenario(store, seats=1)
    approve(store, "one")
    approve(store, "two", BOB)

    async def race():
        await asyncio.gather(*(worker(store).run_until_idle() for _ in range(2)))
    asyncio.run(race())
    assert {store.get(k)["execution"] for k in ("one", "two")} == {"succeeded", "waiting_resource"}
    assert read(store, "check_license_inventory")["available_seats"] == 0
    assert sorted(effects(store)) == ["add_access_group", "assign_license"]
    scenario(store, seats=1)
    store.clock.t += 61
    asyncio.run(worker(store).run_until_idle())
    assert all(store.get(k)["execution"] == "succeeded" for k in ("one", "two"))


@pytest.mark.parametrize("fault", ["group_failure", "after_commit_before_response"])
def test_resume(store, fault):
    class Fault(LocalDirectoryAdapter):
        fired = False

        async def apply(self, operation, action):
            if fault == "group_failure" and action["tool_name"] == "add_access_group" and not self.fired:
                self.fired = True
                raise TransientError("暂时不可用")
            result = await super().apply(operation, action)
            if fault == "after_commit_before_response" and not self.fired:
                self.fired = True
                raise AmbiguousError("提交后响应丢失")
            return result

    approve(store, "a")
    w = worker(store, Fault(store.directory))
    asyncio.run(w.run_once())
    first, second = store.apply_actions("a")
    assert first["status"] == ("succeeded" if fault == "group_failure" else "unknown")
    assert second["status"] == "queued"
    assert effects(store) == ["assign_license"]
    asyncio.run(worker(store).run_until_idle())
    assert store.get("a")["execution"] == "succeeded"
    assert sorted(effects(store)) == ["add_access_group", "assign_license"]
    assert read(store, "check_license_inventory")["available_seats"] == 4
    assert read(store, "check_software_access")["has_access"]
    assert store.apply_actions("a")[0]["attempts"] == (1 if fault == "group_failure" else 2)


def test_policy_version(store):
    approve(store, "old")
    with pytest.raises(ApprovalConflict):
        store.decide("old", "approved", BOB, "stale")
    store.policy = AccessPolicy({**store.policy.data, "version": "changed"})
    asyncio.run(worker(store).run_until_idle())
    assert store.get("old")["execution"] == "failed"
    assert effects(store) == []


def test_repair_and_diagnostics(store):
    repair = agent_tools(lambda: store)["propose_access_repair"].handler
    req = SimpleNamespace(principal=ALICE)
    with pytest.raises(PolicyViolation, match="license_scope_denied"):
        repair(req, {"role": "standard"})
    approve(store, "license")
    asyncio.run(worker(store).run_until_idle())
    with pytest.raises(PolicyViolation, match="already_member"):
        repair(req, {"role": "standard"})
    scenario(store, member=False, outage=True)
    for args, reason in [({"role": "pro"}, "license_scope_denied"),
                         ({"role": "standard", "employee_id": BOB.employee_id}, "self_only")]:
        with pytest.raises(PolicyViolation, match=reason):
            repair(req, args)
    draft = repair(req, {"role": "standard"})
    summary = approval_from_traces([{"tool_name": "propose_access_repair", "input": {"role": "standard"},
                                     "output": draft, "success": True}], "请修复 DevSuite 访问组")
    approve(store, "repair", request=summary["action_requests"][0])
    asyncio.run(worker(store).run_until_idle())
    assert store.get("repair")["execution"] == "succeeded"
    assert read(store, "check_software_access")["has_access"]
    assert read(store, "check_license_inventory")["available_seats"] == 4
    assert read(store, "check_service_health")["status"] == "down"
    scenario(store, member=False, active=False)
    assert not read(store, "check_employee_status")["active"]
    with pytest.raises(PolicyViolation, match="account_unavailable"):
        repair(req, {"role": "standard"})
    scenario(store, expired=True)
    with pytest.raises(PolicyViolation, match="license_scope_denied"):
        repair(req, {"role": "standard"})


def test_receipt_binding_and_independent_readback(store):
    from servicedesk_agent.infra.tenant_context import tenant_scope
    from servicedesk_agent.workflow.adapters.base import PermanentError

    approve(store, "a")
    asyncio.run(worker(store).run_until_idle())
    first, second = store.apply_actions("a")
    spec = {"tool_name": first["tool_name"], "arguments": first["arguments"]}
    with tenant_scope(ALICE.tenant):
        assert store.directory.apply(first["id"], spec)["action"] == "assign_license"
        with pytest.raises(PermanentError, match="operation_conflict"):
            store.directory.apply(first["id"], {**spec, "arguments": {**spec["arguments"], "role": "pro"}})
        scenario(store, member=False)
        with pytest.raises(AmbiguousError):
            store.directory.observe(second["id"], second)
    assert read(store, "check_license_inventory")["available_seats"] == 4


def test_expired_seat_reassigned_without_repair_authority(store):
    scenario(store, seats=1)
    approve(store, "first")
    asyncio.run(worker(store).run_until_idle())
    scenario(store, expired=True)
    assert not read(store, "check_software_access")["has_access"]
    assert read(store, "check_license_inventory")["available_seats"] == 1
    approve(store, "next", BOB)
    asyncio.run(worker(store).run_until_idle())
    assert store.get("next")["execution"] == "succeeded"
    assert read(store, "check_license_inventory")["available_seats"] == 0


def test_pro_requires_its_own_hash_and_two_votes(store):
    old = store.create("standard", request_id="one", conv_id="c", principal=ALICE,
                       action_requests=[access("1001", "standard", "DevSuite")], ask_requester=True)
    pro = store.create("pro", request_id="two", conv_id="c", principal=ALICE,
                       action_requests=[access("1001", "pro", "DevSuite")], ask_requester=True)
    store.confirm("pro", ALICE, pro["payload_hash"])
    with pytest.raises(ApprovalConflict):
        store.decide("pro", "approved", BOB, old["payload_hash"])
    store.decide("pro", "approved", BOB, pro["payload_hash"])
    assert not store.apply_actions("pro")
    store.decide("pro", "approved", CAROL, pro["payload_hash"])
    asyncio.run(worker(store).run_until_idle())
    state = read(store, "check_software_access")
    assert state["has_access"] and state["scope"] == "pro"
    assert state["expires_at"] == store.clock() + 14 * 86400


def test_operator_resumes_only_group_after_account_restored(store):
    from tests.test_workflow import ERIN

    approve(store, "partial")
    w = worker(store)
    asyncio.run(w.run_once(limit=1))
    scenario(store, active=False)
    asyncio.run(w.run_until_idle())
    assert [row["status"] for row in store.apply_actions("partial")] == ["succeeded", "manual_review"]
    scenario(store, active=True)
    store.requeue("partial", ERIN, "账户已恢复，核对访问组")
    asyncio.run(w.run_until_idle())
    assert store.get("partial")["execution"] == "succeeded"
    assert [row["attempts"] for row in store.apply_actions("partial")] == [1, 1]
    assert read(store, "check_license_inventory")["available_seats"] == 4


def test_disabled_employee_ticket_and_grant_preconditions(store):
    from servicedesk_agent.infra.tenant_context import tenant_scope
    from servicedesk_agent.workflow.adapters.base import PermanentError

    scenario(store, active=False)
    args = {"employee_id": ALICE.employee_id, "title": "登录故障", "description": "核查账户停用"}
    ticket = {"tool_name": "create_ticket", "arguments": args}
    with tenant_scope(ALICE.tenant):
        store.directory.preflight(ticket)
        assert store.directory.apply("incident", ticket)["ticket_id"]
        for tool in ("request_access", "assign_license", "propose_access_repair"):
            with pytest.raises(PermanentError):
                store.directory.preflight({"tool_name": tool, "arguments": args})
        with pytest.raises(PermanentError):
            store.directory.apply("grant", {"tool_name": "request_access", "arguments": args})
        absent = {**ticket, "arguments": {**args, "employee_id": "not-in-directory"}}
        with pytest.raises(PermanentError):
            store.directory.preflight(absent)
        with pytest.raises(PermanentError):
            store.directory.apply("missing", absent)
