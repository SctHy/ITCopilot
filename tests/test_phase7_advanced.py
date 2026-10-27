"""Offline Phase 7 boundary tests; no network or real model calls."""
import asyncio
import os
import subprocess
import sys
import tempfile

import pytest
from pydantic_ai.exceptions import ModelConnectionError, ModelHTTPError
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from agents.agent_orchestrator import AgentOrchestrator, AgentType, Request, RoutingDecision
from core.approval import approval_summary
from core.intent_calibration import calibrate, ece, brier
from core.intent_recognizer import IntentCategory, IntentRecognizer
from core.planner import ExecutionPlan, PlanStep, build_plan
from core.redaction import redact
from memory.conversation_memory import MemoryManager
from tooling.knowledge_base import KnowledgeBase
from tooling.tool_manager import CircuitBreaker, CircuitState, MCPToolManager, Tool


@pytest.mark.parametrize("message,agent", [("配置 VPN", "network_software"), ("配置 vpn", "network_software"), ("连接 WIFI", "network_software"), ("连接 wifi", "network_software"), ("处理 MFA", "access"), ("处理 mfa", "access")])
def test_planner_normalizes_english_keywords(message, agent):
    assert any(step.agent_type == agent for step in build_plan(message, "x").steps)


@pytest.mark.parametrize("value", [0.01, 0.25, 0.5, 0.99])
def test_calibration_probability_is_bounded(value):
    assert 0 < calibrate(value, 0.75) < 1


@pytest.mark.parametrize("rows", [[(.9, True), (.2, False)], [(.6, True), (.5, False)], [(.1, False)]])
def test_calibration_metrics_are_finite(rows):
    assert 0 <= ece(rows) <= 1
    assert 0 <= brier(rows) <= 1


@pytest.mark.parametrize("key", ["password", "secret", "authorization", "api_key"])
def test_redaction_removes_sensitive_keys(key):
    assert redact({key: "hidden"})[key] == "[REDACTED]"


def test_memory_profile_merge_preserves_old_values():
    old = MemoryManager._normalize_profile({"department": "研发", "systems": ["GitLab"]})
    new = MemoryManager._normalize_profile({"department": "产品", "systems": ["Jira"]})
    merged = MemoryManager._merge_profile(old, new)
    assert merged["systems"] == ["GitLab", "Jira"]
    assert any(f["value"] == "研发" and f["status"] == "superseded" for f in merged["facts"])


@pytest.mark.parametrize("texts,budget,expected", [(["a" * 100], 128, 1), (["a" * 100, "b" * 100], 128, 2), (["a" * 10, "b" * 10], 128, 2)])
def test_memory_token_budget(texts, budget, expected):
    assert len(MemoryManager._budget_texts(texts, budget)) == expected


def test_memory_sensitive_profile_is_redacted():
    profile = MemoryManager._normalize_profile({"systems": ["password=secret"], "entities": {"token": ["secret-token"]}})
    assert profile["systems"] == ["[REDACTED]"]
    assert profile["entities"]["token"] == ["[REDACTED]"]


def test_rag_vector_bm25_hybrid_and_citation():
    os.environ["IT_COPILOT_SEED_DEMO_DOCS"] = "false"
    with tempfile.TemporaryDirectory() as path:
        kb = KnowledgeBase(chroma_path=path)
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


def test_planner_dependency_wave_contract():
    plan = build_plan("申请权限并准备新电脑", "p")
    ticket = next(step for step in plan.steps if step.step_id == "ticket")
    assert "endpoint" in ticket.depends_on and "access" not in ticket.depends_on


def test_planner_step_limit():
    assert len(build_plan("权限、设备、VPN、软件、工单", "p").steps) <= 6


def test_planner_dependency_cycle_stops_without_running_agents():
    async def run():
        orchestrator = AgentOrchestrator("sk-local")
        plan = ExecutionPlan("p", "goal", [PlanStep("a", "a", "triage", ["b"]), PlanStep("b", "b", "triage", ["a"])])
        result = await orchestrator._run_plan(Request("x"), plan)
        assert result.status == "failed" and result.error_code == "plan_dependency_cycle" and result.escalated
        assert all(a.stats.total == 0 for agents in orchestrator._pool.values() for a in agents)
    asyncio.run(run())


def test_orchestrator_routes_ticket():
    assert AgentOrchestrator("sk-local")._route(IntentCategory.TICKET_STATUS) == AgentType.TICKET


def test_request_has_department_and_budget_fields():
    request = Request("VPN", department="研发")
    assert request.department == "研发" and request.request_id


def test_agent_surfaces_gateway_connection_failure():
    async def run():
        orchestrator = AgentOrchestrator("sk-local", base_url="http://127.0.0.1:8082")
        agent = orchestrator._pool[AgentType.NETWORK_SOFTWARE][0]

        async def refuse(messages, info):
            raise ModelConnectionError("test", "connection refused", phase="connect")

        agent._llm = FunctionModel(refuse)
        result = await agent.handle(Request("VPN 连不上"))
        assert result.error_code == "gateway_unavailable"
        assert "网关未启动" in result.content

    asyncio.run(run())


def test_agent_surfaces_gateway_5xx_without_request_details():
    async def run():
        orchestrator = AgentOrchestrator("sk-local", base_url="http://127.0.0.1:8082")
        agent = orchestrator._pool[AgentType.TRIAGE][0]

        async def bad_gateway(messages, info):
            raise ModelHTTPError(502, "test", body="upstream unavailable")

        agent._llm = FunctionModel(bad_gateway)
        result = await agent.handle(Request("你是谁"))
        assert result.error_code == "gateway_unavailable"
        assert "upstream" not in result.content

    asyncio.run(run())


def test_sse_route_is_registered():
    import api.main
    assert any(route.path == "/chat/stream" for route in api.main.app.routes)


def test_sse_stream_emits_status_and_completed_events():
    async def run():
        import api.main
        original = api.main.chat
        async def fake_chat(payload):
            return api.main.ChatResponse(request_id="r1", response="已完成", status="success", agent_type="triage", intent="chitchat")
        api.main.chat = fake_chat
        try:
            stream = await api.main.chat_stream(api.main.ChatRequest(message="你是谁"))
            chunks = [chunk async for chunk in stream.body_iterator]
            text = "".join(chunk.decode() if isinstance(chunk, bytes) else chunk for chunk in chunks)
            assert "event: status" in text and "event: content_delta" in text and "event: completed" in text
        finally:
            api.main.chat = original
    asyncio.run(run())


def test_trace_route_is_registered():
    import api.main
    assert any(route.path == "/trace/{request_id}" for route in api.main.app.routes)


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


def test_local_embedding_is_stable_across_processes():
    code = "from core.intent_recognizer import IntentRecognizer as R; print(R._local_embed('VPN 连不上'))"
    runs = {subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, env={**os.environ, "PYTHONHASHSEED": seed}).stdout for seed in ("1", "2")}
    assert len(runs) == 1


def test_trace_tools_route_is_not_shadowed_by_request_id_route():
    import api.main
    from fastapi.testclient import TestClient
    assert TestClient(api.main.app).get("/trace/tools").json() == {"items": []}


def test_parallel_run_reports_failure_when_every_agent_crashes():
    async def run():
        orchestrator = AgentOrchestrator("sk-local")
        async def crash(req, agent_type):
            raise RuntimeError("boom")
        orchestrator._execute = crash
        decision = RoutingDecision(AgentType.NETWORK_SOFTWARE, [AgentType.TRIAGE])
        result = await orchestrator.run_parallel(Request("VPN 和权限"), decision)
        assert result.status == "failed" and result.error_code == "agent_failed" and result.response and result.agent_types == []
    asyncio.run(run())


def test_chat_response_exposes_knowledge_used_flag_for_sse_dump():
    import api.main
    base = dict(request_id="r2", response="ok", status="success", agent_type="network_software", intent="vpn_issue")
    no_kb = api.main.ChatResponse(**base, tools_used=["lookup_error_code"])
    with_kb = api.main.ChatResponse(**base, tools_used=["search_knowledge_base"], knowledge_used=True)
    assert no_kb.knowledge_used is False
    assert with_kb.knowledge_used is True
    assert with_kb.model_dump()["knowledge_used"] is True

