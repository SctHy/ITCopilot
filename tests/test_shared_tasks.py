"""Shared-room boundaries and event recovery; SQLite/PostgreSQL, no real model."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, insert

from api import state
from api.main import app
from core.concurrency import LeaseLostError
from core.db import approvals, task_runs, task_members
from tests.conftest import auth
from tests.test_workflow import ALICE, BOB, CAROL, ERIN, Clock, access
from workflow.adapters import AdapterRegistry
from workflow.adapters.local import LocalDirectoryAdapter
from workflow.store import ApprovalForbidden, WorkflowStore
from workflow.tasks import TaskConflict, TaskForbidden, TaskStore
from workflow.task_worker import TaskWorker
from workflow.task_approvals import ApprovalEventSource, TaskApprovalEffects
from workflow.task_access import TaskAccess
from workflow.worker import ExecutionWorker


@pytest.fixture
def env(db_url):
    clock = Clock()
    workflow = WorkflowStore(db_url, clock=clock)
    tasks = TaskStore(workflow.engine, effects=TaskApprovalEffects(workflow), clock=clock)
    tasks.access = TaskAccess(tasks)
    workflow.shared_access, workflow.shared_filter = tasks.access.approval_access, tasks.access.approval_filter
    yield tasks, workflow, clock
    workflow.close()


def seed_participants(tasks, task_id):
    """Fixture rooms have explicitly invited reviewers; raw create stays private by default."""
    if tasks.access is None:
        tasks.access = TaskAccess(tasks)
    for person in (ALICE, BOB, CAROL, ERIN):
        tasks.access.observe(person)
    with tasks.transaction(task_id) as (db, _):
        for person in (BOB, CAROL):
            if not db.execute(select(task_members.c.subject).where(task_members.c.task_id == task_id, task_members.c.subject == person.subject)).first():
                db.execute(insert(task_members).values(task_id=task_id, subject=person.subject, role='collaborator', profile=person.public()))


def room(tasks, client_id="one"):
    task = tasks.create(ALICE, "申请与排查", "申请 GitLab 只读权限，开通后继续排查访问问题", client_id)
    seed_participants(tasks, task['id'])
    return task


def proposal():
    return {"response": "请审批", "proposal": {"action_requests": [access()]}}


def submit(tasks, workflow, task_id, requester=ALICE):
    """The requester confirms the Agent's drafted request so approvers can act on it (ADR 0020)."""
    draft = tasks.get(task_id, requester)["approvals"][0]
    assert draft["status"] == "draft"
    return workflow.confirm(draft["approval_id"], requester, draft["payload_hash"])


def test_three_roles_share_transcript_but_no_other_employee_or_tenant_can_enter(env):
    tasks, _, _ = env
    created = room(tasks)
    tasks.post(created["id"], BOB, "请补充用途，同意只是讨论", "bob-1", False)
    tasks.finish(tasks.claim(), {"response": "已整理申请"})
    applicant, reviewer = tasks.get(created["id"], ALICE), tasks.get(created["id"], BOB)
    assert applicant["events"] == reviewer["events"]
    assert [e["kind"] for e in applicant["events"]] == ["requester", "approver", "assistant"]
    assert all("principal" not in e["data"] for e in applicant["events"])
    assert tasks.get(created["id"], ERIN)["id"] == created["id"]
    for outsider in (replace(ALICE, subject="dev:dave"), replace(BOB, subject="uninvited", department="finance"), replace(ERIN, tenant="other")):
        assert tasks.list(outsider) == []
        with pytest.raises(TaskForbidden):
            tasks.get(created["id"], outsider)
        with pytest.raises(TaskForbidden):
            tasks.post(created["id"], outsider, "我加入", "x")


def test_concurrent_replays_create_one_room_one_message_and_one_run(env):
    tasks, _, _ = env
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda _: room(tasks)["id"], range(8)))
    assert len(set(ids)) == 1
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: tasks.post(ids[0], ALICE, "补充说明", "same"), range(8)))
    body = tasks.get(ids[0], ALICE)
    assert len(body["events"]) == len(body["runs"]) == 2
    with pytest.raises(TaskConflict):
        tasks.post(ids[0], ALICE, "改成管理员权限", "same")
    with pytest.raises(TaskConflict):
        tasks.create(ALICE, "改名", "另一个需求", "one")


def test_expired_claim_cannot_commit_reply_or_approval_and_restart_reclaims(env):
    tasks, workflow, clock = env
    created = room(tasks)
    old = tasks.claim(5)
    clock.t += 6
    restarted = TaskStore(workflow.engine, effects=TaskApprovalEffects(workflow), clock=clock)
    current = restarted.claim(5)
    assert current["id"] == old["id"] and current["version"] > old["version"]
    with pytest.raises(LeaseLostError):
        tasks.finish(old, proposal())
    assert not tasks.get(created["id"], ALICE)["approvals"]
    restarted.finish(current, proposal())
    assert len(tasks.get(created["id"], ALICE)["approvals"]) == 1
    with pytest.raises(LeaseLostError):
        restarted.finish(current, proposal())


def test_lease_expiring_during_commit_rolls_back_approval_and_reply(env, monkeypatch):
    tasks, workflow, clock = env
    created = room(tasks)
    claim = tasks.claim(5)
    original = workflow.create

    def expire(*args, **kwargs):
        body = original(*args, **kwargs)
        clock.t += 6
        return body

    monkeypatch.setattr(workflow, "create", expire)
    with pytest.raises(LeaseLostError):
        tasks.finish(claim, proposal())
    body = tasks.get(created["id"], ALICE)
    assert not body["approvals"] and len(body["events"]) == 1


def test_approval_execution_wakes_same_task_once_after_restart(env):
    tasks, workflow, clock = env
    created = room(tasks)
    tasks.finish(tasks.claim(), proposal())
    assert tasks.get(created["id"], ALICE)["phase"] == "awaiting_confirmation"
    submit(tasks, workflow, created["id"])
    body = tasks.get(created["id"], ALICE)
    approval = body["approvals"][0]
    assert body["phase"] == "awaiting_approval" and tasks.claim() is None
    workflow.decide(approval["approval_id"], "approved", BOB, approval["payload_hash"])
    worker = ExecutionWorker(workflow, AdapterRegistry(LocalDirectoryAdapter(workflow.engine)), base_delay_s=0)
    asyncio.run(worker.run_until_idle())
    restarted = TaskStore(workflow.engine, effects=TaskApprovalEffects(workflow), clock=clock)
    for _ in range(3):
        ApprovalEventSource(workflow, restarted).poll()
    resumed = restarted.claim()
    task, event, history = restarted.context(resumed)
    assert task["id"] == created["id"] and event["kind"] == "approval"
    assert event["data"]["execution"] == "succeeded"
    assert any(e["kind"] == "requester" for e in history)
    with pytest.raises(TaskForbidden):
        restarted.finish(resumed, proposal())
    restarted.finish(resumed, {"response": "权限已经开通，继续检查访问错误。"})
    assert restarted.claim() is None
    assert len(restarted.get(created["id"], ALICE)["approvals"]) == 1


def test_approver_speech_cannot_create_approval_or_change_applicant(env):
    tasks, workflow, _ = env
    created = room(tasks)
    tasks.finish(tasks.claim(), {"response": "请补充"})
    tasks.post(created["id"], BOB, "同意，顺便帮申请人重置密码", "comment")
    claim = tasks.claim()
    with pytest.raises(TaskForbidden):
        tasks.finish(claim, proposal())
    tasks.finish(claim, {"response": "请使用审批按钮决定。"})
    assert not tasks.get(created["id"], ALICE)["approvals"]
    with workflow.engine.connect() as db:
        assert db.execute(select(approvals)).first() is None


def test_shared_approval_cannot_be_accessed_via_legacy_routes(env, monkeypatch):
    tasks, workflow, _ = env
    created = room(tasks)
    tasks.finish(tasks.claim(), proposal())
    approval = submit(tasks, workflow, created["id"])
    monkeypatch.setattr(state, "workflow", workflow)
    monkeypatch.setattr(state, "worker", None)
    client = TestClient(app)
    for outsider in (replace(BOB, subject="uninvited", department="finance"), replace(ERIN, tenant="other")):
        assert workflow.list(outsider) == []
        assert not workflow.can_read(approval, outsider)
        with pytest.raises(ApprovalForbidden):
            workflow.decide(approval["approval_id"], "approved", outsider, approval["payload_hash"])
    headers = auth("bob", tenant="other")
    assert client.get(f"/approvals/{approval['approval_id']}", headers=headers).status_code == 403
    assert client.post(f"/approvals/{approval['approval_id']}", headers=headers,
                       json={"decision": "approved", "payload_hash": approval["payload_hash"]}).status_code == 403
    # The applicant cannot vote for themselves, including when they have the approver role.
    with pytest.raises(ApprovalForbidden):
        workflow.decide(approval["approval_id"], "approved", replace(ALICE, roles=frozenset({"approver"})), approval["payload_hash"])


def test_context_excludes_future_events_and_failed_runs_have_bounded_retry(env):
    tasks, _, clock = env
    created = room(tasks)
    first = tasks.claim()
    tasks.post(created["id"], ALICE, "未来的新消息", "future")
    assert "未来的新消息" not in str(tasks.context(first))
    assert tasks.claim() is None  # same room is serialized
    tasks.fail(first, "temporary")
    assert tasks.claim() is None
    for _ in range(2):
        clock.t += 40
        tasks.fail(tasks.claim(), "temporary")
    body = tasks.get(created["id"], ALICE)
    assert body["runs"][0]["status"] == "failed"
    tasks.retry(created["id"], first["id"], BOB)
    assert tasks.claim()["id"] == first["id"]


def test_stop_fences_inflight_agent_but_does_not_cancel_approved_business_work(env):
    tasks, _, _ = env
    created = room(tasks)
    claim = tasks.claim()
    with pytest.raises(TaskForbidden):
        tasks.stop(created["id"], BOB)
    tasks.stop(created["id"], ALICE)
    assert not tasks.renew(claim, 120)
    with pytest.raises(LeaseLostError):
        tasks.finish(claim, proposal())
    assert tasks.claim() is None
    assert tasks.get(created["id"], ALICE)["phase"] == "stopped"


def test_http_three_party_flow_and_body_identity_is_ignored(env, monkeypatch):
    tasks, workflow, _ = env
    monkeypatch.setattr(state, "tasks", tasks)
    monkeypatch.setattr(state, "workflow", workflow)
    monkeypatch.setattr(state, "worker", None)
    monkeypatch.setattr(state, "task_worker", None)
    client = TestClient(app)
    response = client.post("/tasks", headers=auth(), json={"title": "申请权限", "message": "申请只读",
        "client_request_id": "http", "requester": "dev:bob", "roles": ["it_admin"]})
    assert response.status_code == 201
    task = response.json()
    assert task["requester"] == "dev:alice"
    assert client.get(f"/tasks/{task['id']}", headers=auth("dave")).status_code == 403
    assert client.get(f"/tasks/{task['id']}", headers=auth("bob")).status_code == 403
    tasks.access.observe(BOB)
    tasks.access.set_member(task['id'], ALICE, BOB.subject, 'collaborator')
    assert client.get(f"/tasks/{task['id']}", headers=auth("bob")).status_code == 200
    tasks.finish(tasks.claim(), proposal())
    body = client.get(f"/tasks/{task['id']}", headers=auth("bob")).json()
    approval = body["approvals"][0]
    path = f"/tasks/{task['id']}/approvals/{approval['approval_id']}"
    vote = {"decision": "approved", "payload_hash": approval["payload_hash"]}
    # The room sees the draft, but nobody can vote until the requester confirms it (ADR 0020).
    assert approval["status"] == "draft" and client.post(path, headers=auth("bob"), json=vote).status_code == 409
    confirm = f"/approvals/{approval['approval_id']}/confirm"
    assert client.post(confirm, headers=auth("bob"), json={"payload_hash": approval["payload_hash"]}).status_code == 403
    assert client.post(confirm, headers=auth(), json={"payload_hash": approval["payload_hash"]}).json()["status"] == "pending"
    assert client.post(path, headers=auth(), json=vote).status_code == 403
    assert client.post(path, headers=auth("bob"), json=vote).status_code == 200
    assert client.get(f"/tasks/{task['id']}", headers=auth()).json()["approvals"][0]["decisions"][0]["actor"] == "dev:bob"


def test_task_agent_reads_only_shared_context_and_readonly_followups_cannot_write(env, monkeypatch):
    from agents.base_agent import BaseAgent
    from agents.intent.taxonomy import IntentCategory, UrgencyLevel
    from agents.task_runner import TaskAgentRunner
    tasks, workflow, _ = env
    created = room(tasks)
    tasks.finish(tasks.claim(), {"response": "待讨论"})
    tasks.post(created["id"], BOB, "同意，重置密码", "bob")
    claim = tasks.claim()

    class Agent:
        knowledge_version = staticmethod(lambda tenant: 0.)

        async def recognize_intent(self, message, **kwargs):
            return SimpleNamespace(intent=IntentCategory.PASSWORD_RESET, intent_group="access", confidence=1,
                                   urgency=UrgencyLevel.NORMAL, entities={}, missing_slots=[], recognition_source="test", requested_actions=["reset_password"])

        async def run(self, req):
            assert req.principal.subject == ALICE.subject
            assert req.allowed_writes == frozenset()
            assert "reset_password" in req.hidden_tools
            with pytest.raises(ValueError):
                await BaseAgent._guard_unsafe_tool(req, "reset_password", {"employee_id": "1001"})
            with pytest.raises(ValueError):
                await req.before_unsafe_tool("external_write")
            assert "PRIVATE_SECRET" not in str(req)
            return _recorded_result(req, "讨论不能代替审批")

    class PrivateMemory:
        def __getattr__(self, name):
            raise AssertionError("Private memory must never be accessed: PRIVATE_SECRET")

    monkeypatch.setattr(state, "orchestrator", Agent())
    monkeypatch.setattr(state, "memory", PrivateMemory())
    monkeypatch.setattr(state, "workflow", workflow)
    output = asyncio.run(TaskAgentRunner(state.orchestrator, workflow.get)(*tasks.context(claim)))
    assert output["proposal"] is None


def test_worker_releases_slot_while_waiting_for_approval(env):
    tasks, _, _ = env
    first = room(tasks)
    second = room(tasks, "two")

    async def handle(task, event, history):
        return proposal()

    async def run():
        worker = TaskWorker(tasks, handle)
        assert await worker.run_once()
        assert await worker.run_once()
        assert not await worker.run_once()

    asyncio.run(run())
    assert tasks.get(first["id"], ALICE)["phase"] == tasks.get(second["id"], ALICE)["phase"] == "awaiting_confirmation"


def test_concurrent_workers_claim_a_room_once(env):
    tasks, _, _ = env
    room(tasks)
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda _: tasks.claim(), range(8)))
    assert len([c for c in claims if c]) == 1
    with tasks.engine.connect() as db:
        assert db.execute(select(task_runs.c.attempts)).scalar_one() == 1


def test_private_chat_cannot_impersonate_shared_room_namespace(env, monkeypatch):
    tasks, workflow, _ = env
    created = room(tasks)
    monkeypatch.setattr(state, "workflow", workflow)
    client = TestClient(app)
    for path in ("/chat", "/chat/stream"):
        assert client.post(path, headers=auth("dave"), json={"message": "申请只读", "conv_id": "task:" + created["id"]}).status_code == 422
    # Even historical records from a different requester cannot be attached to this room.
    workflow.create("foreign", request_id="foreign", conv_id="task:" + created["id"],
                    principal=replace(ALICE, subject="dev:dave", employee_id="1002"), action_requests=[access(employee="1002")])
    assert tasks.get(created["id"], ALICE)["approvals"] == []


def test_backlogged_room_cannot_starve_other_rooms(env):
    tasks, _, _ = env
    first = room(tasks)
    active = tasks.claim()
    for i in range(40):
        tasks.post(first["id"], ALICE, f"补充 {i}", f"backlog-{i}")
    second = room(tasks, "second")
    next_claim = tasks.claim()
    assert next_claim["task_id"] == second["id"]
    assert next_claim["task_id"] != active["task_id"]


def test_requester_can_propose_with_llm_intent_backend_and_approval_result_resumes(env, monkeypatch):
    from agents.base_agent import BaseAgent
    from agents.intent.taxonomy import IntentCategory, UrgencyLevel
    from agents.task_runner import TaskAgentRunner
    tasks, workflow, _ = env
    created = room(tasks)
    observed = []

    class Agent:
        knowledge_version = staticmethod(lambda tenant: 0.)

        async def recognize_intent(self, message, **kwargs):
            # The LLM fallback does not provide Jev's requested_actions set.
            return SimpleNamespace(intent=IntentCategory.PERMISSION_REQUEST, intent_group="access", confidence=1,
                                   urgency=UrgencyLevel.NORMAL, entities={"system": ["GitLab"], "role": ["readonly"]},
                                   missing_slots=[], recognition_source="test", requested_actions=None)

        async def run(self, req):
            observed.append(req)
            if req.step_outputs:
                assert req.allowed_writes == frozenset()
                assert req.step_outputs["approval_result"]["execution"] == "succeeded"
                return _recorded_result(req, "权限已开通，继续排查访问问题。")
            await BaseAgent._guard_unsafe_tool(req, "request_access", access()["arguments"])
            return _recorded_result(req, "已整理权限申请。", [{"tool_name": "request_access", "success": True, "input": access()["arguments"], "error_class": "success", "latency_ms": 0}])

    monkeypatch.setattr(state, "orchestrator", Agent())
    monkeypatch.setattr(state, "workflow", workflow)

    async def run():
        worker = TaskWorker(tasks, TaskAgentRunner(state.orchestrator, workflow.get))
        assert await worker.run_once()
        approval = submit(tasks, workflow, created["id"])
        assert approval["requester"] == ALICE.subject
        workflow.decide(approval["approval_id"], "approved", BOB, approval["payload_hash"])
        executor = ExecutionWorker(workflow, AdapterRegistry(LocalDirectoryAdapter(workflow.engine)), base_delay_s=0)
        await executor.run_until_idle()
        ApprovalEventSource(workflow, tasks).poll()
        assert await worker.run_once()
        assert not await worker.run_once()

    asyncio.run(run())
    assert len(observed) == 2
    assert len(tasks.get(created["id"], ALICE)["approvals"]) == 1


def test_task_layer_runs_other_events_without_approval_service_or_api(env):
    _, workflow, clock = env
    tasks = TaskStore(workflow.engine, clock=clock)  # No business-effects adapter.
    created = room(tasks)
    tasks.finish(tasks.claim(), {"response": "等待诊断结果"})
    event = dict(source="diagnostics", object_id="check-42", version=2, kind="external",
                 text="诊断已完成", data={"reachable": True})
    assert tasks.ingest(created["id"], **event)
    assert not tasks.ingest(created["id"], **event)
    assert not tasks.ingest(created["id"], **{**event, "version": 1, "text": "旧状态"})
    restarted = TaskStore(workflow.engine, clock=clock)
    observed = []

    async def handle(task, trigger, history):
        observed.append(trigger)
        return {"response": "根据最新诊断继续处理"}

    async def run():
        worker = TaskWorker(restarted, handle)
        assert await worker.run_once()
        assert not await worker.run_once()

    asyncio.run(run())
    assert len(observed) == 1 and observed[0]["data"]["reachable"] is True
    assert restarted.get(created["id"], ALICE)["events"][-1]["text"] == "根据最新诊断继续处理"


def test_source_failure_does_not_block_other_sources_or_existing_work(env):
    from workflow.task_events import TaskEventPump
    tasks, _, _ = env
    created = room(tasks)

    class Broken:
        def poll(self):
            raise RuntimeError("upstream unavailable")

    class Healthy:
        def poll(self):
            tasks.ingest(created["id"], source="healthy", object_id="status", version=1, kind="external", text="恢复")

    async def run():
        pump = TaskEventPump([Broken(), Healthy()])
        await pump.run_once()
        handled = []

        async def handle(task, event, history):
            handled.append(event["kind"])
            return {"response": "已处理"}

        worker = TaskWorker(tasks, handle)
        while await worker.run_once():
            pass
        assert handled == ["requester", "external"]

    asyncio.run(run())


def test_each_formal_vote_is_recorded_once_even_when_poll_happens_after_execution(env):
    from workflow.approval_service import ApprovalService
    from tests.test_workflow import CAROL
    tasks, workflow, _ = env
    created = room(tasks)
    tasks.finish(tasks.claim(), {"response": "需要双人审批", "proposal": {"action_requests": [access(role="developer")]}})
    approval = submit(tasks, workflow, created["id"])
    service = ApprovalService(workflow)
    asyncio.run(service.decide(approval["approval_id"], "approved", approval["payload_hash"], BOB))
    assert workflow.get(approval["approval_id"])["status"] == "pending"
    asyncio.run(service.decide(approval["approval_id"], "approved", approval["payload_hash"], CAROL))
    executor = ExecutionWorker(workflow, AdapterRegistry(LocalDirectoryAdapter(workflow.engine)), base_delay_s=0)
    asyncio.run(executor.run_until_idle())
    source = ApprovalEventSource(workflow, tasks)
    source.poll(); source.poll()
    votes = [e for e in tasks.get(created["id"], ALICE)["events"] if e["kind"] == "approval_vote"]
    assert [v["actor"] for v in votes] == [BOB.subject, CAROL.subject]
    assert all("同意审批" in v["text"] for v in votes)
    assert tasks.context(tasks.claim())[1]["kind"] == "approval"
    assert tasks.claim() is None
    calls = []
    tasks.ingest = lambda *a, **k: calls.append(k)
    source.poll()
    assert calls == []  # already reflected rows are filtered in SQL, not re-ingested every tick


def test_rejection_wakes_original_task_without_executing_business_action(env):
    from workflow.approval_service import ApprovalService
    tasks, workflow, _ = env
    created = room(tasks)
    tasks.finish(tasks.claim(), proposal())
    approval = submit(tasks, workflow, created["id"])
    asyncio.run(ApprovalService(workflow).decide(approval["approval_id"], "rejected", approval["payload_hash"], BOB))
    ApprovalEventSource(workflow, tasks).poll()
    event = tasks.context(tasks.claim())[1]
    assert event["kind"] == "approval" and event["data"]["status"] == "rejected"
    assert workflow.get(approval["approval_id"])["results"] == []


def test_stopping_agent_keeps_formal_votes_and_business_results_visible(env):
    tasks, workflow, _ = env
    created = room(tasks)
    tasks.finish(tasks.claim(), proposal())
    approval = submit(tasks, workflow, created["id"])
    tasks.stop(created["id"], ALICE)
    workflow.decide(approval["approval_id"], "approved", BOB, approval["payload_hash"])
    executor = ExecutionWorker(workflow, AdapterRegistry(LocalDirectoryAdapter(workflow.engine)), base_delay_s=0)
    asyncio.run(executor.run_until_idle())
    ApprovalEventSource(workflow, tasks).poll()
    body = tasks.get(created["id"], ALICE)
    assert body["phase"] == "stopped"
    assert body["approvals"][0]["execution"] == "succeeded"
    assert any(e["kind"] == "approval_vote" and e["actor"] == BOB.subject for e in body["events"])
    assert body["events"][-1]["data"]["execution"] == "succeeded"
    assert tasks.claim() is None


def _recorded_result(req, text, traces=()):
    from agents.base_agent import BaseAgent
    from agents.types import OrchestratorResult
    req.route_metadata = {"model": "deepseek-flash", "route": "agent_write", "route_reason": "primary", "cost_usd": 0.0}
    return OrchestratorResult(req.request_id, text, req.intent, tool_traces=list(traces),
                              observability=BaseAgent(None, "test")._observability(req))
