"""End-to-end /chat through the real FastAPI handler with a FunctionModel LLM; no Redis, no network."""
import asyncio

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart

import api.main as api
from agents.agent_orchestrator import AgentOrchestrator
from api import schemas, state
from api.routes import chat as chat_routes
from evaluation.fake_model import scripted_model
from monitor.performance_monitor import PerformanceMonitor
from tests.conftest import auth
from tooling.tool_manager import MCPToolManager


def sample(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


@pytest.fixture
def chat_client(monkeypatch):
    async def respond(messages, info):
        if info.output_tools:  # 意图识别的结构化输出
            return ModelResponse(parts=[ToolCallPart("classify_it_request", {"intent": "vpn_issue", "confidence": 0.9, "entities": {"error_code": ["VPN-691"]}})])
        if not any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            return ModelResponse(parts=[ToolCallPart("lookup_error_code", {"error_code": "VPN-691"})])
        return ModelResponse(parts=[TextPart("VPN-691 表示认证失败，请确认账号未锁定。")])

    model = scripted_model(respond)
    orchestrator = AgentOrchestrator("sk-local")
    orchestrator.planning_mode = "single"  # these tests assert specialist routing and per-agent health
    orchestrator._intent_recognizer.llm = model
    for agents in orchestrator._pool.values():
        for agent in agents:
            agent._llm = model
    tool_manager = MCPToolManager("sk-local")
    monitor = PerformanceMonitor(orchestrator, tool_manager)
    for name, value in {"orchestrator": orchestrator, "memory": None, "tool_manager": tool_manager, "monitor": monitor}.items():
        monkeypatch.setattr(state, name, value)
    monkeypatch.setenv("IT_INTENT_LLM_ENABLED", "true")
    return TestClient(api.app, headers=auth("alice")), monitor


def test_chat_end_to_end_records_metrics_and_slo(chat_client):
    http, monitor = chat_client
    before = sample("it_copilot_chat_requests_total", status="success", intent_group="network")
    agent_stage = sample("it_copilot_chat_stage_seconds_count", stage="agent") or 0
    response = http.post("/chat", json={"message": "VPN 报错 VPN-691", "client_request_id": "e2e-1"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "success" and body["agent_type"] == "network_software" and body["intent"] == "vpn_issue"
    assert body["tools_used"] == ["lookup_error_code"] and "VPN-691" in body["response"]
    assert sample("it_copilot_chat_requests_total", status="success", intent_group="network") == before + 1
    # 无记忆、无审批时只有 memory_context / intent / agent 三段。
    assert set(body["stage_ms"]) == {"memory_context", "intent", "agent"} and all(v >= 0 for v in body["stage_ms"].values())
    assert sample("it_copilot_chat_stage_seconds_count", stage="agent") == agent_stage + 1
    assert monitor.slos["chat_availability"].window(300) == (1, 0)


def test_chat_stream_end_to_end(chat_client):
    http, _ = chat_client
    with http.stream("POST", "/chat/stream", json={"message": "VPN 报错 VPN-691", "client_request_id": "e2e-2"}) as response:
        text = "".join(response.iter_text())
    assert "event: tool" in text and "event: completed" in text and "event: error" not in text
    # 正文边生成边推送；调用工具那一轮之后清草稿，最终答案的文字才以 answer_delta 出现。
    assert text.index("event: answer_reset") < text.index("event: answer_delta") < text.index("event: completed")


def test_stream_request_key_binds_message_and_conversation(chat_client, monkeypatch):
    from core.events import InMemoryEventBus
    monkeypatch.setattr(state, "events", InMemoryEventBus())
    http, _ = chat_client
    payload = {"message": "VPN 报错 VPN-691", "conv_id": "binding", "client_request_id": "bound-key"}
    first = http.post("/chat/stream", json=payload)
    assert first.status_code == 200 and "event: completed" in first.text
    assert http.post("/chat/stream", json=payload).text == first.text
    assert http.post("/chat/stream", json={**payload, "message": "changed"}).status_code == 409
    assert http.post("/chat/stream", json={**payload, "conv_id": "changed"}).status_code == 409


def test_monitor_endpoint_reports_live_state(chat_client):
    http, monitor = chat_client
    http.post("/chat", json={"message": "VPN 报错 VPN-691", "client_request_id": "e2e-3"})
    monitor.tick()
    body = http.get("/monitor").json()
    assert body["agents"]["network_software_0"]["state"] == "healthy"
    assert body["monitor"]["status"] == "ok" and body["monitor"]["slos"][0]["requests_1h"] >= 1
    assert body["mcp"] == {"enabled": False, "servers": []}


def test_second_turn_reuses_employee_id_from_conversation_state(chat_client, monkeypatch):
    from memory.conversation_memory import MemoryContext

    class FakeMemory:
        async def get_context(self, user_id, conv_id, query=""):
            return MemoryContext([], [], {}, "", state={"intent_group": "access", "entities": {"employee_id": ["E1001"]}})

        async def commit_turn(self, *args, **kwargs):
            pass

    monkeypatch.setattr(state, "memory", FakeMemory())
    monkeypatch.setenv("IT_INTENT_LLM_ENABLED", "false")
    from core.identity import principal_from_token
    alice = principal_from_token(auth("alice")["Authorization"].split()[1])
    _, req, intent_result, _ = asyncio.run(chat_routes._run_turn(schemas.ChatRequest(message="帮我重置密码"), alice, "turn-2", "c1", chat_routes._Stages()))
    assert "employee_id" in intent_result.missing_slots          # 本轮消息本身没有工号
    assert "employee_id" not in req.conversation_state["missing_fields"]
    assert req.entities["employee_id"] == ["E1001"]


def test_login_employee_id_fills_the_slot_when_no_turn_provided_one(chat_client, monkeypatch):
    from memory.conversation_memory import MemoryContext

    class FakeMemory:
        def __init__(self, entities):
            self.entities = entities

        async def get_context(self, user_id, conv_id, query=""):
            return MemoryContext([], [], {}, "", state={"intent_group": "access", "entities": self.entities})

        async def commit_turn(self, *args, **kwargs):
            pass

    monkeypatch.setenv("IT_INTENT_LLM_ENABLED", "false")
    from core.identity import principal_from_token
    alice = principal_from_token(auth("alice")["Authorization"].split()[1])

    def turn(message, entities):
        monkeypatch.setattr(state, "memory", FakeMemory(entities))
        _, req, _, _ = asyncio.run(chat_routes._run_turn(schemas.ChatRequest(message=message), alice, "t", "c1", chat_routes._Stages()))
        return req

    ticket = turn("帮我建个工单，打印机坏了", {})
    assert ticket.entities["employee_id"] == [alice.employee_id] and "employee_id" not in ticket.conversation_state["missing_fields"]
    explicit = turn("帮工号 E2002 建个工单", {})                       # 消息里写明的工号优先（IT 管理员代办）
    assert explicit.entities["employee_id"] == ["E2002"]
    reset = turn("帮我重置密码", {})                                   # 身份敏感操作仍要用户明确给出工号
    assert "employee_id" not in reset.entities and "employee_id" in reset.conversation_state["missing_fields"]
