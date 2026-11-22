"""Private web chat history: recorded per settled turn, redacted, owner-only, deletable and swept."""
import pytest
from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from sqlalchemy import select

from tests import runtime as api
from it_copilot.agent.agent_orchestrator import AgentOrchestrator
from tests.runtime import state
from it_copilot.infra import conversation_history as history
from it_copilot.infra.db import admin_audit, conversation_messages, conversations
from it_copilot.infra.events import InMemoryEventBus
from it_copilot.infra.retention import DAY, RetentionPolicy, RetentionSweeper
from it_copilot.infra.tenant_context import tenant_scope
from tests.support.fake_model import scripted_model
from tests.conftest import auth, with_read_tool
from tests.test_workflow import ALICE, BOB
from it_copilot.workflow.store import WorkflowStore


@pytest.fixture
def store(db_url):
    return WorkflowStore(db_url)


@pytest.fixture
def http(store, monkeypatch):
    async def respond(messages, info):
        if info.output_tools:
            return ModelResponse(parts=[ToolCallPart("classify_it_request", {"intent": "vpn_issue", "confidence": .9, "entities": {}})])
        return ModelResponse(parts=[TextPart("请先确认账号未被锁定。")])
    orchestrator = with_read_tool(AgentOrchestrator("sk-local"))
    orchestrator.agent._llm = orchestrator._intent_recognizer.llm = scripted_model(respond)
    for name, value in {"orchestrator": orchestrator, "workflow": store, "memory": None, "kb": None,
                        "request_recall": None, "monitor": None, "events": InMemoryEventBus()}.items():
        monkeypatch.setattr(state, name, value)
    monkeypatch.setenv("IT_INTENT_LLM_ENABLED", "true")
    return TestClient(api.app)


def chat(http, message, conv_id, user="alice", request_key=None):
    body = {"message": message, "conv_id": conv_id, **({"client_request_id": request_key} if request_key else {})}
    response = http.post("/chat", json=body, headers=auth(user))
    assert response.status_code == 200, response.text
    return response.json()


def test_settled_turns_are_listed_and_reopened_with_redacted_text(http):
    chat(http, "VPN 连不上，我的密码是 Abc12345! 手机 13812345678", "c1")
    chat(http, "还是不行", "c1")
    chat(http, "另一个问题", "c2")

    listed = http.get("/conversations", headers=auth("alice")).json()
    assert [item["conv_id"] for item in listed["items"]] == ["c2", "c1"] and listed["has_more"] is False
    first = next(item for item in listed["items"] if item["conv_id"] == "c1")
    assert first["message_count"] == 4 and "Abc12345" not in first["title"] and "[REDACTED]" in first["title"]

    opened = http.get("/conversations/c1", headers=auth("alice")).json()
    assert [m["role"] for m in opened["messages"]] == ["user", "assistant", "user", "assistant"]
    assert "Abc12345" not in opened["messages"][0]["content"] and "13812345678" not in opened["messages"][0]["content"]
    assert opened["messages"][1]["content"] == "请先确认账号未被锁定。"
    assert opened["messages"][1]["meta"]["intent"] == "vpn_issue"

    page = http.get("/conversations", params={"limit": 1}, headers=auth("alice")).json()
    assert len(page["items"]) == 1 and page["has_more"] is True
    rest = http.get("/conversations", params={"limit": 1, "before": page["items"][0]["updated_at"]}, headers=auth("alice")).json()
    assert [item["conv_id"] for item in rest["items"]] == ["c1"]
    assert [i["conv_id"] for i in http.get("/conversations", params={"q": "还是不行"}, headers=auth("alice")).json()["items"]] == ["c1"]


def test_a_replayed_request_is_recorded_once(http):
    chat(http, "VPN 连不上", "c1", request_key="same")
    chat(http, "VPN 连不上", "c1", request_key="same")
    assert http.get("/conversations/c1", headers=auth("alice")).json()["message_count"] == 2


@pytest.mark.parametrize("other", ["bob", "erin"])
def test_nobody_else_can_read_rename_or_delete_a_private_conversation(http, other):
    chat(http, "VPN 连不上", "c1")
    assert http.get("/conversations", headers=auth(other)).json()["items"] == []
    assert http.get("/conversations/c1", headers=auth(other)).status_code == 404
    assert http.patch("/conversations/c1", json={"title": "x"}, headers=auth(other)).status_code == 404
    assert http.delete("/conversations/c1", headers=auth(other)).status_code == 404
    assert http.get("/conversations/c1", headers=auth("alice")).status_code == 200


def test_owner_renames_and_deletes_with_an_audit_entry(http, store):
    chat(http, "VPN 连不上", "c1")
    assert http.patch("/conversations/c1", json={"title": "VPN 排障"}, headers=auth("alice")).status_code == 200
    assert http.get("/conversations", headers=auth("alice")).json()["items"][0]["title"] == "VPN 排障"
    assert http.delete("/conversations/c1", headers=auth("alice")).json() == {"deleted": True}
    assert http.get("/conversations/c1", headers=auth("alice")).status_code == 404
    with tenant_scope("default"), store.engine.connect() as db:
        assert db.execute(select(conversation_messages)).all() == []
        entry = db.execute(select(admin_audit).where(admin_audit.c.event == "chat_history_deleted")).mappings().one()
    assert entry["subject"] == "c1" and entry["actor"] == ALICE.subject and entry["detail"] == {"messages": 2}


def test_other_channels_are_not_recorded(store):
    from it_copilot.api.routes.chat import _record_history
    from it_copilot.api.schemas import ChatResponse
    import asyncio
    state_workflow = state.workflow
    state.workflow = store
    try:
        reply = ChatResponse(request_id="r1", response="ok", status="success", intent="other", intent_group="other")
        asyncio.run(_record_history(state, ALICE, "feishu:oc", "r1", "feishu", "hi", reply))
        asyncio.run(_record_history(state, ALICE, "", "r2", "web", "hi", reply))
    finally:
        state.workflow = state_workflow
    with tenant_scope("default"), store.engine.connect() as db:
        assert db.execute(select(conversations)).all() == []


def test_retention_sweeps_old_history_and_keeps_active_conversations(store):
    now = 1_800_000_000.0
    with store.engine.begin() as db:
        for conv_id, age in (("old", 100), ("active", 100)):
            history.record_exchange(db, ALICE, conv_id, f"{conv_id}-r1", "web", "问题", {"response": "回答"}, now=now - age * DAY)
        history.record_exchange(db, ALICE, "active", "active-r2", "web", "追问", {"response": "回答"}, now=now - DAY)
        history.record_exchange(db, BOB, "bob", "bob-r1", "web", "问题", {"response": "回答"}, now=now - DAY)
    report = RetentionSweeper(store.engine, RetentionPolicy(history_days=90, batch=1), clock=lambda: now).sweep()
    assert report["chat_history"] == 4 and report["chat_conversations"] == 1
    with store.engine.connect() as db:
        left = {r.conv_id for r in db.execute(select(conversations.c.conv_id))}
        active = history.get_conversation(db, ALICE, "active")
    assert left == {"active", "bob"} and [m["content"] for m in active["messages"]] == ["追问", "回答"]


def test_same_subject_in_another_tenant_sees_nothing(http):
    chat(http, "VPN 连不上", "c1")
    other = auth("alice", tenant="other")
    assert http.get("/conversations", headers=other).json()["items"] == []
    assert http.get("/conversations/c1", headers=other).status_code == 404
    assert http.patch("/conversations/c1", json={"title": "x"}, headers=other).status_code == 404
    assert http.delete("/conversations/c1", headers=other).status_code == 404
    assert http.get("/conversations/c1", headers=auth("alice")).json()["title"] == "VPN 连不上"
