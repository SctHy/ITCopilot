"""End-to-end /chat through the real FastAPI handler with a FunctionModel LLM; no Redis, no network."""
import asyncio

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

import api.main as api
from agents.agent_orchestrator import AgentOrchestrator
from monitor.performance_monitor import PerformanceMonitor
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

    model = FunctionModel(respond)
    orchestrator = AgentOrchestrator("sk-local")
    orchestrator._intent_recognizer.llm = model
    for agents in orchestrator._pool.values():
        for agent in agents:
            agent._llm = model
    tool_manager = MCPToolManager("sk-local")
    monitor = PerformanceMonitor(orchestrator, tool_manager)
    for name, value in {"_orchestrator": orchestrator, "_memory": None, "_tool_manager": tool_manager, "_monitor": monitor}.items():
        monkeypatch.setattr(api, name, value)
    monkeypatch.setenv("IT_INTENT_LLM_ENABLED", "true")
    return TestClient(api.app), monitor


def test_chat_end_to_end_records_metrics_and_slo(chat_client):
    http, monitor = chat_client
    before = sample("it_copilot_chat_requests_total", status="success", intent_group="network")
    response = http.post("/chat", json={"message": "VPN 报错 VPN-691", "client_request_id": "e2e-1"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "success" and body["agent_type"] == "network_software" and body["intent"] == "vpn_issue"
    assert body["tools_used"] == ["lookup_error_code"] and "VPN-691" in body["response"]
    assert sample("it_copilot_chat_requests_total", status="success", intent_group="network") == before + 1
    assert monitor.slos["chat_availability"].window(300) == (1, 0)


def test_chat_stream_end_to_end(chat_client):
    http, _ = chat_client
    with http.stream("POST", "/chat/stream", json={"message": "VPN 报错 VPN-691", "client_request_id": "e2e-2"}) as response:
        text = "".join(response.iter_text())
    assert "event: tool" in text and "event: completed" in text and "event: error" not in text


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

    monkeypatch.setattr(api, "_memory", FakeMemory())
    monkeypatch.setenv("IT_INTENT_LLM_ENABLED", "false")
    _, req, intent_result, _ = asyncio.run(api._run_turn(api.ChatRequest(message="帮我重置密码"), "turn-2", "c1"))
    assert "employee_id" in intent_result.missing_slots          # 本轮消息本身没有工号
    assert "employee_id" not in req.conversation_state["missing_fields"]
    assert req.entities["employee_id"] == ["E1001"]
