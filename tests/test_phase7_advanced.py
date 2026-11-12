"""Offline Phase 7 boundary tests; no network or real model calls."""
import asyncio
import os
import tempfile

import pytest
from pydantic_ai.exceptions import ModelConnectionError, ModelHTTPError
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from agents.agent_orchestrator import AgentOrchestrator
from agents.intent.recognizer import IntentRecognizer
from agents.types import Request
from core.redaction import redact
from memory.conversation_memory import MemoryManager
from tooling.knowledge_base import KnowledgeBase
from tooling.tool_manager import CircuitBreaker, CircuitState, MCPToolManager, Tool
from workflow.approval import approval_summary


@pytest.mark.parametrize("key", ["password", "secret", "authorization", "api_key"])
def test_redaction_removes_sensitive_keys(key):
    assert redact({key: "hidden"})[key] == "[REDACTED]"


def test_memory_profile_merge_preserves_old_values():
    old = MemoryManager._normalize_profile({"background": "研发部", "communication": "中文，简短"})
    new = MemoryManager._normalize_profile({"background": "产品部"})
    merged = MemoryManager._merge_profile(old, new)
    # 修订版漏写的字段沿用旧值；写了的字段整句替换。
    assert merged["communication"] == "中文，简短" and merged["background"] == "产品部"


@pytest.mark.parametrize("texts,budget,expected", [(["a" * 100], 128, 1), (["a" * 100, "b" * 100], 128, 2), (["a" * 10, "b" * 10], 128, 2)])
def test_memory_token_budget(texts, budget, expected):
    assert len(MemoryManager._budget_texts(texts, budget)) == expected


def test_memory_sensitive_profile_is_redacted():
    profile = MemoryManager._normalize_profile({"work": "password=secret", "environment": "token abc"})
    assert profile["work"] == "[REDACTED]" and profile["environment"] == "[REDACTED]"


def test_rag_vector_bm25_hybrid_and_citation():
    os.environ["IT_COPILOT_SEED_DEMO_DOCS"] = "false"
    with tempfile.TemporaryDirectory() as path:
        kb = KnowledgeBase(f"sqlite:///{path}/kb.sqlite3")
        kb.add_documents([{"title": "研发 VPN", "content": "VPN DNS 客户端排障", "department": "研发"}, {"title": "财务 ERP", "content": "ERP 权限审批", "department": "财务"}])
        for mode in ("vector", "bm25", "hybrid"):
            rows = kb.search("VPN DNS", mode=mode, department="研发")
            assert rows and rows[0]["title"] == "研发 VPN" and rows[0]["citation"]
    os.environ.pop("IT_COPILOT_SEED_DEMO_DOCS", None)


@pytest.mark.parametrize("threshold", [1, 2, 3])
def test_circuit_breaker_opens_at_threshold(threshold):
    breaker = CircuitBreaker(failure_threshold=threshold, recovery_s=60)
    for _ in range(threshold): breaker.record_failure()
    assert breaker.state == CircuitState.OPEN and not breaker.allow()


def test_circuit_breaker_half_open_recovers():
    breaker = CircuitBreaker(failure_threshold=1, recovery_s=0)
    breaker.record_failure()
    assert breaker.allow() is True
    breaker.record_success(breaker.generation)
    assert breaker.state == CircuitState.CLOSED


def test_tool_schema_rejects_unknown_fields():
    async def run():
        manager = MCPToolManager("sk-local")
        manager.register(Tool("lookup", "lookup", lambda p, c: p, {"type":"object","properties":{"q":{"type":"string"}},"additionalProperties":False,"required":["q"]}))
        result = await manager.call("lookup", {"q":"x", "unknown":1})
        assert not result.success and "Schema" in result.error
    asyncio.run(run())


def test_tool_fallback_is_used_after_timeout():
    async def run():
        async def slow(p, c): await asyncio.sleep(.02)
        manager = MCPToolManager("sk-local")
        manager.register(Tool("slow", "slow", slow, {"type":"object"}, timeout_s=.001, fallback=lambda p,c,e: {"fallback":True}))
        result = await manager.call("slow", {})
        assert result.data["fallback"] is True
    asyncio.run(run())


def test_approval_is_dry_run_and_risky():
    summary = approval_summary(["request_access"], "申请权限")
    assert summary["risk"] == "high" and "dry-run" in summary["summary"]


def test_approval_ignores_read_only_tools():
    assert approval_summary(["lookup_asset", "knowledge_search"], "查询") is None


def test_request_has_department_and_budget_fields():
    request = Request("VPN", department="研发")
    assert request.department == "研发" and request.request_id


def test_agent_surfaces_model_connection_failure():
    async def run():
        orchestrator = AgentOrchestrator("sk-local", base_url="http://127.0.0.1:9")
        agent = orchestrator.agent

        async def refuse(messages, info):
            raise ModelConnectionError("test", "connection refused", phase="connect")

        agent._llm = FunctionModel(refuse)
        result = await agent.handle(Request("VPN 连不上"))
        assert result.error_code == "model_unavailable"
        assert "模型服务暂时不可用" in result.content

    asyncio.run(run())


def test_agent_surfaces_model_5xx_without_request_details():
    async def run():
        orchestrator = AgentOrchestrator("sk-local", base_url="http://127.0.0.1:9")
        agent = orchestrator.agent

        async def server_error(messages, info):
            raise ModelHTTPError(502, "test", body="upstream unavailable")

        agent._llm = FunctionModel(server_error)
        result = await agent.handle(Request("你是谁"))
        assert result.error_code == "model_unavailable"
        assert "upstream" not in result.content

    asyncio.run(run())


def test_sse_route_is_registered():
    import api.main
    assert "/chat/stream" in api.main.app.openapi()["paths"]


def test_sse_stream_emits_status_and_completed_events(monkeypatch):
    from fastapi.testclient import TestClient

    import api.main
    from api import schemas, state
    from api.routes import chat as chat_routes
    from core.events import InMemoryEventBus
    from tests.conftest import auth

    async def fake_chat(payload, principal, rid, emit=None):
        await emit("tool_started", {"tool": "diagnose"})
        return schemas.ChatResponse(request_id=rid, response="已完成", status="success", intent="chitchat")

    monkeypatch.setattr(state, "orchestrator", object())
    monkeypatch.setattr(chat_routes, "_chat", fake_chat)
    monkeypatch.setattr(state, "events", InMemoryEventBus())
    with TestClient(api.main.app, headers=auth()).stream("POST", "/chat/stream", json={"message": "你是谁"}) as response:
        text = "".join(response.iter_text())
    assert text.index("event: status") < text.index("event: tool_started") < text.index("event: content_delta") < text.index("event: completed")
    assert "id: " in text


def test_trace_route_is_registered():
    import api.main
    assert "/trace/{request_id}" in api.main.app.openapi()["paths"]


def _classifier_model(mode):
    async def respond(messages, info):
        if mode == "error":
            raise ValueError("bad payload")
        return ModelResponse(parts=[ToolCallPart("classify_it_request", {"intent": "vpn_issue", "confidence": 0.9})])
    return FunctionModel(respond)


@pytest.mark.parametrize("llm_enabled,mode,source", [("true", "ok", "llm_tool_use"), ("false", "ok", "fallback_disabled"), ("true", "error", "fallback_exception")])
def test_intent_recognition_source_reflects_llm_path(monkeypatch, llm_enabled, mode, source):
    monkeypatch.setenv("IT_INTENT_LLM_ENABLED", llm_enabled)
    recognizer = IntentRecognizer("sk-local")
    recognizer.llm = _classifier_model(mode)
    result = asyncio.run(recognizer.recognize("VPN 连不上"))
    assert result.recognition_source == source and result.llm_used is (source == "llm_tool_use")


@pytest.mark.parametrize("message,previous_group,expected,inherited", [
    ("还是不行", "network", "network_issue", True),
    ("它怎么用", "network", "network_issue", True),
    ("那个账号被锁了", "access", "account_unlock", True),
    ("第二个怎么处理", "ticket", "ticket_status", True),
    ("这个 VPN 怎么配置", "access", "vpn_issue", False),
    ("还是不行", None, "other", False),
])
def test_followup_inherits_previous_group_only_without_topic_switch(monkeypatch, message, previous_group, expected, inherited):
    monkeypatch.setenv("IT_INTENT_LLM_ENABLED", "false")
    result = asyncio.run(IntentRecognizer("sk-local").recognize(message, previous_intent_group=previous_group))
    assert result.intent.value == expected and result.inherited_from_previous is inherited


@pytest.mark.parametrize("message,urgency", [("全员无法上网，生产中断", "critical"), ("紧急！VPN 完全不能用", "high"), ("账号被锁定了", "high"), ("VPN 连不上", "normal")])
def test_urgency_rules_apply_when_llm_unavailable(monkeypatch, message, urgency):
    monkeypatch.setenv("IT_INTENT_LLM_ENABLED", "false")
    assert asyncio.run(IntentRecognizer("sk-local").recognize(message)).urgency.value == urgency


def test_trace_tools_route_is_not_shadowed_by_request_id_route():
    from fastapi.testclient import TestClient

    import api.main
    from tests.conftest import auth
    client = TestClient(api.main.app)
    assert client.get("/trace/tools", headers=auth("grace")).json() == {"items": []}   # auditor
    assert client.get("/trace/tools", headers=auth("alice")).status_code == 403


def test_chat_response_exposes_knowledge_used_flag_for_sse_dump():
    from api import schemas
    base = dict(request_id="r2", response="ok", status="success", intent="vpn_issue")
    no_kb = schemas.ChatResponse(**base, tools_used=["diagnose"])
    with_kb = schemas.ChatResponse(**base, tools_used=["search_knowledge_base"], knowledge_used=True)
    assert no_kb.knowledge_used is False
    assert with_kb.knowledge_used is True
    assert with_kb.model_dump()["knowledge_used"] is True

