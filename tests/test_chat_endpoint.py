"""End-to-end /chat through the real FastAPI handler with a FunctionModel LLM; no Redis, no network."""
import asyncio

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart

from tests import runtime as api
from servicedesk_agent.agent.agent_orchestrator import AgentOrchestrator
from servicedesk_agent.api import schemas
from tests.runtime import state
from servicedesk_agent.api.routes import chat as chat_routes
from tests.support.fake_model import scripted_model
from tests.conftest import auth
from servicedesk_agent.agent.tooling.tool_manager import ToolManager
from tests.conftest import with_read_tool


def sample(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


@pytest.fixture
def chat_client(monkeypatch):
    async def respond(messages, info):
        if info.output_tools:  # 意图识别的结构化输出
            return ModelResponse(parts=[ToolCallPart("classify_it_request", {"intent": "vpn_issue", "confidence": 0.9, "entities": {"error_code": ["VPN-691"]}})])
        if not any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            return ModelResponse(parts=[ToolCallPart("diagnose", {"code": "VPN-691"})])
        return ModelResponse(parts=[TextPart("VPN-691 表示认证失败，请确认账号未锁定。")])

    model = scripted_model(respond)
    orchestrator = with_read_tool(AgentOrchestrator("sk-local"))
    orchestrator._intent_recognizer.llm = model
    orchestrator.agent._llm = model
    tool_manager = ToolManager("sk-local")
    for name, value in {"orchestrator": orchestrator, "memory": None, "tool_manager": tool_manager}.items():
        monkeypatch.setattr(state, name, value)
    monkeypatch.setenv("IT_INTENT_LLM_ENABLED", "true")
    return TestClient(api.app, headers=auth("alice"))


def test_chat_end_to_end_records_metrics(chat_client):
    http = chat_client
    before = sample("servicedesk_agent_chat_requests_total", status="success", intent_group="network")
    agent_stage = sample("servicedesk_agent_chat_stage_seconds_count", stage="agent") or 0
    response = http.post("/chat", json={"message": "VPN 报错 VPN-691", "client_request_id": "e2e-1"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "success" and body["intent"] == "vpn_issue"
    assert body["tools_used"] == ["diagnose"] and "VPN-691" in body["response"]
    assert sample("servicedesk_agent_chat_requests_total", status="success", intent_group="network") == before + 1
    # 无记忆、无审批时只有 memory_context / intent / agent 三段。
    assert set(body["stage_ms"]) == {"memory_context", "intent", "agent"} and all(v >= 0 for v in body["stage_ms"].values())
    assert sample("servicedesk_agent_chat_stage_seconds_count", stage="agent") == agent_stage + 1


def test_chat_stream_end_to_end(chat_client):
    http = chat_client
    with http.stream("POST", "/chat/stream", json={"message": "VPN 报错 VPN-691", "client_request_id": "e2e-2"}) as response:
        text = "".join(response.iter_text())
    assert "event: tool" in text and "event: completed" in text and "event: error" not in text
    # 正文边生成边推送；调用工具那一轮之后清草稿，最终答案的文字才以 answer_delta 出现。
    assert text.index("event: answer_reset") < text.index("event: answer_delta") < text.index("event: completed")


def test_stream_request_key_binds_message_and_conversation(chat_client, monkeypatch):
    from servicedesk_agent.infra.events import InMemoryEventBus
    monkeypatch.setattr(state, "events", InMemoryEventBus())
    http = chat_client
    payload = {"message": "VPN 报错 VPN-691", "conv_id": "binding", "client_request_id": "bound-key"}
    first = http.post("/chat/stream", json=payload)
    assert first.status_code == 200 and "event: completed" in first.text
    assert http.post("/chat/stream", json=payload).text == first.text
    assert http.post("/chat/stream", json={**payload, "thinking": "low"}).status_code == 409
    assert http.post("/chat/stream", json={**payload, "message": "changed"}).status_code == 409
    assert http.post("/chat/stream", json={**payload, "conv_id": "changed"}).status_code == 409


def test_second_turn_reuses_employee_id_from_conversation_state(chat_client, monkeypatch):
    from servicedesk_agent.agent.memory.conversation_memory import MemoryContext

    class FakeMemory:
        async def get_context(self, user_id, conv_id, query="", **kwargs):
            return MemoryContext([], [], "", state={"intent_group": "access", "entities": {"employee_id": ["E1001"]}})

        async def commit_turn(self, *args, **kwargs):
            pass

    monkeypatch.setattr(state, "memory", FakeMemory())
    monkeypatch.setenv("IT_INTENT_LLM_ENABLED", "false")
    from servicedesk_agent.infra.identity import principal_from_token
    alice = principal_from_token(auth("alice")["Authorization"].split()[1])
    _, req, intent_result, _ = asyncio.run(chat_routes._run_turn(state, schemas.ChatRequest(message="帮我重置密码"), alice, "turn-2", "c1", chat_routes._Stages()))
    assert "employee_id" in intent_result.missing_slots          # 本轮消息本身没有工号
    assert "employee_id" not in req.conversation_state["missing_fields"]
    assert req.entities["employee_id"] == ["E1001"]


def test_login_employee_id_fills_the_slot_when_no_turn_provided_one(chat_client, monkeypatch):
    from servicedesk_agent.agent.memory.conversation_memory import MemoryContext

    class FakeMemory:
        def __init__(self, entities):
            self.entities = entities

        async def get_context(self, user_id, conv_id, query="", **kwargs):
            return MemoryContext([], [], "", state={"intent_group": "access", "entities": self.entities})

        async def commit_turn(self, *args, **kwargs):
            pass

    monkeypatch.setenv("IT_INTENT_LLM_ENABLED", "false")
    from servicedesk_agent.infra.identity import principal_from_token
    alice = principal_from_token(auth("alice")["Authorization"].split()[1])

    def turn(message, entities):
        monkeypatch.setattr(state, "memory", FakeMemory(entities))
        _, req, _, _ = asyncio.run(chat_routes._run_turn(state, schemas.ChatRequest(message=message), alice, "t", "c1", chat_routes._Stages()))
        return req

    ticket = turn("帮我建个工单，打印机坏了", {})
    assert ticket.entities["employee_id"] == [alice.employee_id] and "employee_id" not in ticket.conversation_state["missing_fields"]
    explicit = turn("帮工号 E2002 建个工单", {})                       # 消息里写明的工号优先（IT 管理员代办）
    assert explicit.entities["employee_id"] == ["E2002"]
    reset = turn("帮我重置密码", {})                                   # 身份敏感操作仍要用户明确给出工号
    assert "employee_id" not in reset.entities and "employee_id" in reset.conversation_state["missing_fields"]


def test_chat_model_selection_and_policy(chat_client, monkeypatch):
    from servicedesk_agent.agent.model_router import TenantConfig
    from servicedesk_agent.infra.events import InMemoryEventBus
    monkeypatch.setattr(state, "events", InMemoryEventBus())
    routing = state.orchestrator.router
    monkeypatch.setenv("OPENAI_GPT_API_KEY", "synthetic-gpt-test")
    catalog = chat_client.get("/models")
    assert catalog.status_code == 200 and "synthetic-gpt-test" not in catalog.text
    assert catalog.json()["defaults"]["agent_read"] == {"model": "deepseek-flash", "thinking": "low"}
    assert next(m for m in catalog.json()["models"] if m["id"] == "gpt-6.2-sol")["configured"]
    seen = []
    pick = routing.pick
    def capture(*args, **kwargs):
        routed = pick(*args, **kwargs)
        if routed.route.startswith("agent_"):
            seen.append(routed)
        return routed
    monkeypatch.setattr(routing, "pick", capture)
    payload = {"message": "VPN 报错 VPN-691", "model": "deepseek-flash", "thinking": "disabled"}
    for path in ("/chat", "/chat/stream"):
        result = chat_client.post(path, json=payload)
        assert result.status_code == 200 and seen[-1].name == "deepseek-flash"
        assert seen[-1].settings["extra_body"]["thinking"]["type"] == "disabled"
        assert chat_client.post(path, json={**payload, "model": "unknown"}).status_code == 400
        assert chat_client.post(path, json={**payload, "thinking": "unknown"}).status_code == 400
        seen.clear()
    spec = routing.config.models["deepseek-flash"]
    for updates in ({"supports": []}, {"thinking_api": "none"}):
        monkeypatch.setitem(routing.config.models, "deepseek-flash", spec.model_copy(update=updates))
        for path in ("/chat", "/chat/stream"):
            assert chat_client.post(path, json={**payload, "thinking": "high"}).status_code == 400
    monkeypatch.setitem(routing.config.models, "deepseek-flash", spec)
    monkeypatch.setitem(routing.config.tenants, "default", TenantConfig(data_class="internal", regions=["us"]))
    assert [m["id"] for m in chat_client.get("/models").json()["models"]] == ["gpt-6.2-sol"]
    for path in ("/chat", "/chat/stream"):
        assert chat_client.post(path, json=payload).status_code == 403


def test_runtime_default_audited_and_reset(chat_client, db_url, monkeypatch):
    from servicedesk_agent.workflow.store import WorkflowStore
    monkeypatch.setattr(state, "workflow", WorkflowStore(db_url))
    payload = {"route": "agent_write", "model": "gpt-6.2-sol", "thinking": "high"}
    path = "/ops/models/default"
    assert chat_client.post(path, json=payload).status_code == 403
    assert chat_client.post(path, json=payload, headers=auth("erin")).status_code == 200
    assert state.orchestrator.router.pick("agent_write").name == "gpt-6.2-sol"
    defaults = chat_client.get("/models").json()["defaults"]
    assert defaults["agent_write"] == {"model": "gpt-6.2-sol", "thinking": "high"}
    assert defaults["agent_read"] == {"model": "deepseek-flash", "thinking": "low"}
    assert chat_client.post(path, json={"route": "agent_write", "reset": True}, headers=auth("erin")).status_code == 200
    assert state.orchestrator.router.pick("agent_write").name == "deepseek-flash"
    audit = chat_client.get("/admin/audit", params={"event": "model_default_changed"}, headers=auth("erin")).json()
    assert [entry["detail"]["reset"] for entry in audit["items"]] == [True, False]
