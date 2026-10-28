"""Offline observability tests: agent degradation state machine, Prometheus metrics, OpenTelemetry spans."""
import asyncio
import time

from prometheus_client import REGISTRY
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

from agents.agent_orchestrator import AgentOrchestrator, AgentStats, AgentType, Request
from tooling.tool_manager import MCPToolManager, Tool


def sample(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


def reply(text="好的"):
    async def respond(messages, info):
        return ModelResponse(parts=[TextPart(text)], usage=RequestUsage(input_tokens=7, output_tokens=3))
    return FunctionModel(respond)


def orchestrator_with(model):
    orchestrator = AgentOrchestrator("sk-local")
    for agents in orchestrator._pool.values():
        for agent in agents:
            agent._llm = model
    return orchestrator


# ── 降级状态机 ────────────────────────────────────────────────────────────────

def test_agent_stats_state_machine_allows_single_probe():
    stats = AgentStats()
    assert stats.state() == "healthy" and stats.try_acquire("r0")
    stats.set_penalty(.8)
    assert stats.state() == "cooling" and not stats.try_acquire("r1")
    stats.retry_at = time.monotonic() - 1
    assert stats.state() == "half_open"
    assert stats.try_acquire("probe") and stats.state() == "probing"
    assert not stats.try_acquire("other")
    stats.record(True, 10, "probe")
    assert stats.state() == "healthy" and stats.monitor_penalty == 0


def test_failed_probe_returns_to_cooling():
    stats = AgentStats(retry_at=time.monotonic() - 1)
    assert stats.try_acquire("probe")
    stats.record(False, 10, "probe")
    assert stats.state() == "cooling"


def test_degraded_specialist_falls_back_to_triage():
    async def run():
        orchestrator = orchestrator_with(reply("Triage 已接手"))
        network = orchestrator._pool[AgentType.NETWORK_SOFTWARE][0]
        network.stats.set_penalty(.9)
        busy_before = sample("it_copilot_agent_failures_total", agent="network_software", reason="busy")
        response = await orchestrator._execute(Request("VPN 连不上"), AgentType.NETWORK_SOFTWARE)
        assert response.agent_type == AgentType.TRIAGE and response.success and response.content == "Triage 已接手"
        assert network.stats.total == 0
        assert sample("it_copilot_agent_failures_total", agent="network_software", reason="busy") == busy_before + 1
    asyncio.run(run())


# ── Prometheus 指标 ───────────────────────────────────────────────────────────

def test_agent_metrics_record_status_tokens_and_tool_calls():
    async def respond(messages, info):
        if not any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            return ModelResponse(parts=[ToolCallPart("lookup_asset", {"asset_id": "PC-1"})], usage=RequestUsage(input_tokens=5, output_tokens=2))
        return ModelResponse(parts=[TextPart("资产正常")], usage=RequestUsage(input_tokens=6, output_tokens=4))

    async def run():
        orchestrator = orchestrator_with(FunctionModel(respond))
        labels = {"agent": "endpoint", "status": "success", "error_code": "none"}
        before = (sample("it_copilot_agent_requests_total", **labels), sample("it_copilot_llm_tokens_total", agent="endpoint", model="deepseek-chat", direction="input"),
                  sample("it_copilot_agent_tool_calls_total", agent="endpoint", tool="lookup_asset", outcome="success"))
        await orchestrator._execute(Request("查一下资产 PC-1"), AgentType.ENDPOINT)
        assert sample("it_copilot_agent_requests_total", **labels) == before[0] + 1
        assert sample("it_copilot_llm_tokens_total", agent="endpoint", model="deepseek-chat", direction="input") == before[1] + 11
        assert sample("it_copilot_agent_tool_calls_total", agent="endpoint", tool="lookup_asset", outcome="success") == before[2] + 1
    asyncio.run(run())


def test_agent_failure_reason_is_classified():
    async def loop_forever(messages, info):
        return ModelResponse(parts=[ToolCallPart("check_service_status", {"service": "vpn"})])

    async def run():
        orchestrator = orchestrator_with(FunctionModel(loop_forever))
        before = sample("it_copilot_agent_failures_total", agent="network_software", reason="round_limit")
        response = await orchestrator._execute(Request("VPN 状态"), AgentType.NETWORK_SOFTWARE)
        assert response.error_code == "agent_failed"
        assert sample("it_copilot_agent_failures_total", agent="network_software", reason="round_limit") == before + 1
    asyncio.run(run())


def test_tool_manager_metrics_label_every_outcome():
    async def run():
        manager = MCPToolManager("sk-local")
        schema = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"], "additionalProperties": False}

        async def slow(params, ctx):
            await asyncio.sleep(.05)

        manager.register(Tool("obs_echo", "echo", lambda p, c: p, schema, cache_ttl=60))
        manager.register(Tool("obs_slow", "slow", slow, schema, timeout_s=.001))
        manager._tools["obs_slow"].breaker.threshold = 1
        before = {o: sample("it_copilot_tool_calls_total", tool="obs_echo", outcome=o) for o in ("success", "cached", "invalid")}
        await manager.call("obs_echo", {"q": "x"})
        await manager.call("obs_echo", {"q": "x"})
        await manager.call("obs_echo", {"bad": 1})
        for outcome in before:
            assert sample("it_copilot_tool_calls_total", tool="obs_echo", outcome=outcome) == before[outcome] + 1
        await manager.call("obs_slow", {"q": "x"})
        await manager.call("obs_slow", {"q": "x"})
        assert sample("it_copilot_tool_calls_total", tool="obs_slow", outcome="timeout") >= 1
        assert sample("it_copilot_tool_calls_total", tool="obs_slow", outcome="circuit_open") >= 1
        assert sample("it_copilot_tool_circuit_state", tool="obs_slow") == 2
    asyncio.run(run())


def test_intent_metrics_count_source_and_clarification(monkeypatch):
    from core.intent_recognizer import IntentRecognizer
    monkeypatch.setenv("IT_INTENT_LLM_ENABLED", "false")
    before = (sample("it_copilot_intent_recognitions_total", source="fallback_disabled", intent_group="access"), sample("it_copilot_intent_clarifications_total", intent_group="access"))
    asyncio.run(IntentRecognizer("sk-local").recognize("帮我重置密码"))
    assert sample("it_copilot_intent_recognitions_total", source="fallback_disabled", intent_group="access") == before[0] + 1
    assert sample("it_copilot_intent_clarifications_total", intent_group="access") == before[1] + 1


def test_metrics_endpoint_exposes_new_series():
    import api.main
    from fastapi.testclient import TestClient
    text = TestClient(api.main.app).get("/metrics").text
    for name in ("it_copilot_agent_requests_total", "it_copilot_tool_calls_total", "it_copilot_intent_recognitions_total", "it_copilot_slo_burn_rate"):
        assert name in text


# ── OpenTelemetry ─────────────────────────────────────────────────────────────

def test_tracing_nests_agent_spans_under_chat_without_content():
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from core import telemetry

    async def respond(messages, info):
        if not any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            return ModelResponse(parts=[ToolCallPart("lookup_error_code", {"error_code": "VPN-691"})])
        return ModelResponse(parts=[TextPart("认证失败")])

    async def run():
        orchestrator = orchestrator_with(FunctionModel(respond))
        with telemetry.chat_span("trace-1"):
            await orchestrator._execute(Request("我的员工号 E1001，VPN-691"), AgentType.NETWORK_SOFTWARE)

    exporter = InMemorySpanExporter()
    provider = telemetry.setup_tracing(exporter)
    try:
        asyncio.run(run())
    finally:
        telemetry.shutdown_tracing(provider)
    spans = exporter.get_finished_spans()
    root = next(s for s in spans if s.name == "it_copilot.chat")
    assert root.attributes["it_copilot.request_id"] == "trace-1"
    children = [s for s in spans if s is not root]
    assert children and all(s.context.trace_id == root.context.trace_id for s in children)
    assert any("lookup_error_code" in s.name for s in children)
    assert not any("E1001" in str(value) for s in spans for value in s.attributes.values())
