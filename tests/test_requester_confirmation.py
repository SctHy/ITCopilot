"""Requester confirmation (ADR 0020): the Agent drafts, the requester submits, approvers decide.

The Agent chose the arguments of a change request; for the actions the policy lists under
``requester_confirmation`` the request starts as a draft that only its requester sees and
can submit. Confirming re-checks the policy like a decision does and binds to the frozen hash.
"""
import asyncio
import dataclasses

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart

from evaluation.application import application
from evaluation.fake_model import scripted_model
from tests.conftest import auth
from tests.test_policy_revalidation import changed, without_gitlab_readonly
from tests.test_workflow import ALICE, BOB, Clock, access, run
from workflow.adapters import AdapterRegistry
from workflow.adapters.local import LocalDirectoryAdapter
from workflow.policy import load_policy
from workflow.store import ApprovalConflict, ApprovalForbidden, ApprovalStateConflict, WorkflowStore
from workflow.worker import ExecutionWorker

GRACE = dataclasses.replace(ALICE, subject="dev:grace", username="grace", employee_id="9002", roles=frozenset({"auditor"}))
TICKET = {"tool_name": "create_ticket", "arguments": {"employee_id": "1001", "title": "VPN 断线", "description": "已重装客户端"}}


@pytest.fixture
def env(db_url):
    clock = Clock()
    notices = []
    store = WorkflowStore(db_url, clock=clock, policy=load_policy(), notify=lambda db, notice: notices.append(notice))
    worker = ExecutionWorker(store, AdapterRegistry(LocalDirectoryAdapter(store.engine)), worker_id="w1", base_delay_s=0)
    return store, worker, clock, notices


def draft(store, approval_id="a", actions=None, conv_id="c"):
    return store.create(approval_id, request_id="r-" + approval_id, conv_id=conv_id, principal=ALICE,
                        action_requests=actions or [access()], ask_requester=True)


def events(store, approval_id):
    return [e["event"] for e in store.audit(approval_id)]


def test_policy_decides_which_requests_start_as_drafts(env):
    store, _, _, _ = env
    assert draft(store)["status"] == "draft"
    assert draft(store, "t", [TICKET])["status"] == "pending"            # a ticket needs no confirmation
    # Callers that cannot show the request back to its requester keep the old behaviour.
    assert store.create("x", request_id="r-x", conv_id="other", principal=ALICE, action_requests=[access()])["status"] == "pending"


def test_a_draft_is_invisible_to_approvers_and_cannot_be_decided(env):
    store, worker, _, notices = env
    body = draft(store)
    assert body["awaiting_confirmation"] and body["confirmed_at"] is None
    with pytest.raises(ApprovalStateConflict):
        store.decide("a", "approved", BOB, body["payload_hash"])
    assert store.list(BOB) == [] and not store.can_read(body, BOB)
    assert [a["approval_id"] for a in store.list(ALICE)] == ["a"] and store.can_read(body, GRACE)  # requester, auditor
    run(worker)
    assert store.employee("1001", "default")["grants"] == [] and notices == []


def test_only_the_requester_confirms_what_they_were_shown(env):
    store, worker, clock, _ = env
    body = draft(store)
    with pytest.raises(ApprovalForbidden):
        store.confirm("a", BOB, body["payload_hash"])
    with pytest.raises(ApprovalForbidden):
        store.confirm("a", dataclasses.replace(ALICE, tenant="other"), body["payload_hash"])
    with pytest.raises(ApprovalConflict):
        store.confirm("a", ALICE, "f" * 64)
    clock.t += 3600
    pending = store.confirm("a", ALICE, body["payload_hash"])
    assert pending["status"] == "pending" and not pending["awaiting_confirmation"] and pending["payload_hash"] == body["payload_hash"]
    # The approver window starts at confirmation, not when the Agent drafted it.
    policy = load_policy()
    assert pending["expires_at"] > body["expires_at"]
    assert store.confirm("a", ALICE, body["payload_hash"])["status"] == "pending"   # idempotent
    assert events(store, "a") == ["created", "confirmed"]
    store.decide("a", "approved", BOB, body["payload_hash"])
    run(worker)
    assert store.employee("1001", "default")["grants"][0]["role"] == "readonly"
    assert policy.confirmation_ttl_hours < policy.approval_ttl_hours


def test_unconfirmed_drafts_lapse_quietly_and_can_be_cancelled(env):
    store, _, clock, notices = env
    draft(store, "old")
    clock.t += load_policy().confirmation_ttl_hours * 3600 + 1
    assert store.expire_pending() == 1 and store.get("old")["status"] == "expired"
    with pytest.raises(ApprovalStateConflict):
        store.confirm("old", ALICE)
    draft(store, "new")
    assert store.withdraw("new", ALICE)["status"] == "withdrawn"
    assert notices == []                     # nobody but the requester ever saw these drafts
    assert store.audit("new")[-1]["detail"] == {"draft": True}


def test_a_new_draft_replaces_drafts_but_a_pending_request_waits_for_confirmation(env):
    store, _, _, _ = env
    pending = draft(store, "p")
    store.confirm("p", ALICE, pending["payload_hash"])
    first = draft(store, "d1")
    second = draft(store, "d2")
    assert store.get("d1")["status"] == "superseded" and second["superseded"] == ["d1"]
    assert store.get("p")["status"] == "pending"           # walking away from d2 would lose nothing
    store.confirm("d2", ALICE)
    assert store.get("p")["status"] == "superseded" and store.get("d2")["status"] == "pending"
    assert first["status"] == "draft"


def test_confirming_rechecks_a_changed_policy(env):
    store, _, _, notices = env
    body = draft(store)
    store.policy = changed(store.policy.declared_version, without_gitlab_readonly)
    assert store.confirm("a", ALICE, body["payload_hash"])["status"] == "invalidated"
    assert "policy_invalidated" in events(store, "a") and notices[-1]["event"] == "invalidated"


def test_requester_items_offer_confirmation_for_drafts(env):
    store, _, _, _ = env
    draft(store)
    assert [(i["approval_id"], i["kind"]) for i in store.requester_items(ALICE)] == [("a", "confirm")]
    assert [d["approval_id"] for d in store.drafts(ALICE, "c")] == ["a"] and store.drafts(BOB, "c") == []


# ── Through the chat API ─────────────────────────────────────────────────────

def counting_access_model(calls):
    async def reply(messages, info):
        calls.append(1)
        if info.output_tools:  # intent classification
            return ModelResponse(parts=[ToolCallPart("classify_it_request", {
                "intent": "permission_request", "confidence": 0.95, "entities": {"employee_id": ["1001"], "system": ["GitLab"]}})])
        prompt = messages[-1].parts[-1]
        asked = isinstance(getattr(prompt, "content", None), str) and "申请 GitLab" in prompt.content
        if asked and not any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            return ModelResponse(parts=[
                ToolCallPart("search_knowledge_base", {"query": "GitLab 权限"}),
                ToolCallPart("request_access", {"employee_id": "1001", "system": "GitLab", "role": "readonly",
                                                "reason": "代码审阅 sk-abcdefghijklmnopqrstuvwx"}),
            ])
        return ModelResponse(parts=[TextPart("已生成申请草稿，请确认后提交审批。")])
    return scripted_model(reply)


def test_chat_drafts_then_a_typed_confirm_submits_without_the_model():
    async def run_case():
        calls = []
        async with application(model=counting_access_model(calls), documents=[{"title": "GitLab 权限 SOP", "content": "GitLab 权限申请流程"}]) as (http, state):
            ask = {"message": "申请 GitLab 只读权限，工号1001，理由代码审阅", "conv_id": "c1", "client_request_id": "ask"}
            body = (await http.post("/chat", json=ask)).json()
            assert body["approval_required"] and body["awaiting_confirmation"]
            approval_id = body["approval_id"]
            assert state.workflow.get(approval_id)["status"] == "draft"
            model_calls = len(calls)

            # "确认" is the requester's decision: handled by code, no model round, no second draft.
            confirmed = (await http.post("/chat", json={"message": "确认", "conv_id": "c1", "client_request_id": "yes"})).json()
            assert len(calls) == model_calls
            assert confirmed["approval_id"] == approval_id and confirmed["approval_required"] and not confirmed["awaiting_confirmation"]
            assert "已确认提交" in confirmed["response"]
            assert state.workflow.get(approval_id)["status"] == "pending"
            assert [e["event"] for e in state.workflow.audit(approval_id)][-1] == "confirmed"

            # Repeating confirmation reports the existing pending request without a new model call.
            await http.post("/chat", json={"message": "确认", "conv_id": "c1", "client_request_id": "again"})
            assert len(calls) == model_calls

            # Why the Agent proposed it is frozen into the immutable "created" entry.
            created = next(e for e in state.workflow.audit(approval_id) if e["event"] == "created")
            provenance = created["detail"]["provenance"]
            assert created["detail"]["awaiting_confirmation"] is True
            assert provenance["request_id"] == body["request_id"] and provenance["intent"]["intent"]
            assert any("GitLab 权限 SOP" in c for c in provenance["knowledge"])
            access_call = next(t for t in provenance["tools"] if t["tool"] == "request_access")
            assert access_call["input"]["role"] == "readonly" and access_call["input"]["reason"] == "代码审阅 [REDACTED]"
            # Not part of what approvers see or the hash they sign.
            assert "provenance" not in state.workflow.get(approval_id)

    asyncio.run(run_case())


def test_chat_cancel_withdraws_the_draft():
    async def run_case():
        async with application(model=counting_access_model([])) as (http, state):
            body = (await http.post("/chat", json={"message": "申请 GitLab 只读权限，工号1001，理由代码审阅", "conv_id": "c2", "client_request_id": "ask"})).json()
            cancelled = (await http.post("/chat", json={"message": "取消", "conv_id": "c2", "client_request_id": "no"})).json()
            assert "已取消申请草稿" in cancelled["response"] and not cancelled["approval_required"]
            assert state.workflow.get(body["approval_id"])["status"] == "withdrawn"
            # The card path binds to the hash; a cancelled draft cannot be confirmed afterwards.
            approval = (await http.get("/approvals/" + body["approval_id"])).json()
            refused = await http.post(f"/approvals/{body['approval_id']}/confirm", json={"payload_hash": approval["payload_hash"]})
            assert refused.status_code == 409
            assert (await http.get("/approvals", params={"status": "draft"}, headers=auth("bob"))).json()["items"] == []

    asyncio.run(run_case())


def test_im_reply_asks_for_confirmation():
    from intake.im import format_reply
    text = format_reply({"response": "已生成申请草稿。", "approval_required": True, "awaiting_confirmation": True, "approval_id": "abcdef1234:approval"})
    assert "回复“确认”" in text and "已提交审批" not in text
    assert "已提交审批" in format_reply({"response": "好的", "approval_required": True, "approval_id": "abcdef1234:approval"})


def test_conversation_approval_states_include_terminal_and_isolate_owner(env):
    store, _, _, _ = env
    draft(store)
    draft(store, "other", conv_id="other")
    assert store.conversation_approvals(ALICE, "c")[0]["status"] == "draft"
    store.confirm("a", ALICE)
    current = store.conversation_approvals(ALICE, "c")
    assert len(current) == 1 and current[0]["status"] == "pending"
    assert current[0]["confirmed_at"] is not None
    assert store.conversation_approvals(BOB, "c") == []
    assert store.conversation_approvals(dataclasses.replace(ALICE, tenant="other"), "c") == []
    store.withdraw("a", ALICE)
    assert store.conversation_approvals(ALICE, "c")[0]["status"] == "withdrawn"


def test_chat_reads_card_confirmation_before_followup():
    prompts = []

    async def reply(messages, info):
        if info.output_tools:
            return ModelResponse(parts=[ToolCallPart("classify_it_request", {
                "intent": "permission_request", "confidence": .9})])
        prompts.append(str(messages))
        return ModelResponse(parts=[TextPart("申请已提交，等待审批。")])

    async def go():
        async with application(model=scripted_model(reply)) as (http, state):
            body = draft(state.workflow, "card-confirm", conv_id="card-conversation")
            response = await http.post("/approvals/card-confirm/confirm",
                                       json={"payload_hash": body["payload_hash"]})
            assert response.status_code == 200, response.text
            for i, message in enumerate(("好了吗", "我提交了")):
                response = await http.post("/chat", json={
                    "message": message, "conv_id": "card-conversation",
                    "client_request_id": f"followup-{i}"})
                assert response.status_code == 200, response.text
            assert state.workflow.get("card-confirm")["status"] == "pending"

    asyncio.run(go())
    assert len(prompts) == 2
    for prompt in prompts:
        assert "本会话本人申请的最新数据库状态" in prompt
        assert "card-confirm" in prompt and '"status": "pending"' in prompt
        assert "不要求重复确认" in prompt


def test_persisted_submission_does_not_excuse_new_or_failed_writes():
    from types import SimpleNamespace
    from agents.prompting import claim_problems
    from agents.types import Request
    from agents.intent.taxonomy import IntentCategory

    item = {"status": "pending", "confirmed_at": 123, "actions": ["request_access"]}
    req = Request("好了吗", intent=IntentCategory.PERMISSION_REQUEST,
                  allowed_writes=frozenset(), conversation_approvals=[item])
    deps = SimpleNamespace(traces=[])
    assert claim_problems("权限申请已提交。", deps, req) == []
    req.allowed_writes = frozenset({"request_access"})
    assert claim_problems("权限申请已提交。", deps, req)
    req.allowed_writes = frozenset()
    item["status"], item["confirmed_at"] = "draft", None
    assert claim_problems("权限申请已提交。", deps, req)


@pytest.mark.parametrize("message", ["对，就这样提交吧", "确认，提交", "确认锁定"])
def test_natural_confirmation_is_not_a_new_application(message):
    from api.routes.chat import _confirmation_verb
    assert _confirmation_verb(message) == "confirm"


def test_action_specific_confirmation_leaves_other_drafts_untouched():
    async def go():
        async with application(model=counting_access_model([])) as (http, state):
            draft(state.workflow, "access", conv_id="two")
            draft(state.workflow, "lock", conv_id="two", actions=[
                {"tool_name": "lock_device", "arguments": {"employee_id": "1001", "device_id": "DEV-1001", "reason": "设备丢失"}}])
            response = await http.post("/chat", json={"message": "确认锁定", "conv_id": "two",
                                                      "client_request_id": "lock-confirm"})
            assert response.status_code == 200, response.text
            assert state.workflow.get("lock")["status"] == "pending"
            assert state.workflow.get("access")["status"] == "draft"
    asyncio.run(go())


def test_card_withdrawal_overrides_old_typed_confirmation_history():
    from types import SimpleNamespace
    from agents.prompting import claim_problems
    from agents.types import Request

    req = Request("我撤回了，现在怎么样", allowed_writes=frozenset(),
        history=[{"role": "assistant", "content": "已生成待审批 a"}],
        conversation_approvals=[{"approval_id": "a", "status": "withdrawn",
                                 "confirmed_at": 123, "actions": ["request_access"]}])
    assert claim_problems("这条权限申请已撤回。", SimpleNamespace(traces=[]), req) == []


def test_drafts_batch_reads_preserve_order_and_requester(env):
    from sqlalchemy import event

    store, _, clock, _ = env
    for i, system in enumerate(('GitLab', 'Jira')):
        draft(store, str(i), [access(system=system)])
        clock.t += 1
    statements = []
    def observe(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith('SELECT'):
            statements.append(statement)
    event.listen(store.engine, 'before_cursor_execute', observe)
    try:
        items = store.drafts(ALICE, 'c')
    finally:
        event.remove(store.engine, 'before_cursor_execute', observe)
    assert [d['approval_id'] for d in items] == ['0', '1']
    assert all(d['description'] and d['requester'] == ALICE.subject for d in items)
    assert len(statements) == 5
    assert store.drafts(BOB, 'c') == []
