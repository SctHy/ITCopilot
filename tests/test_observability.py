"""Offline observability tests: agent stats window, Prometheus metrics, OpenTelemetry spans."""
import asyncio
from types import SimpleNamespace

from prometheus_client import REGISTRY
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

from agents.agent_orchestrator import AgentOrchestrator
from agents.agent_stats import AgentStats
from agents.types import Request
from tooling.tool_manager import MCPToolManager, Tool
from tests.conftest import with_read_tool


def sample(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


def reply(text="好的"):
    async def respond(messages, info):
        return ModelResponse(parts=[TextPart(text)], usage=RequestUsage(input_tokens=7, output_tokens=3))
    return FunctionModel(respond)


def orchestrator_with(model):
    orchestrator = with_read_tool(AgentOrchestrator("sk-local"))
    orchestrator.agent._llm = model
    return orchestrator


# ── 统计窗口 ──────────────────────────────────────────────────────────────────

def test_agent_stats_window_tracks_recent_success_and_latency():
    stats = AgentStats()
    assert stats.recent() == (0, 1., 0.)
    stats.record(True, 100)
    stats.record(False, 300)
    assert stats.recent() == (2, .5, 200.)


# ── Prometheus 指标 ───────────────────────────────────────────────────────────

def test_agent_metrics_record_status_tokens_and_tool_calls():
    async def respond(messages, info):
        if not any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            return ModelResponse(parts=[ToolCallPart("lookup_asset", {"asset_id": "DEV-1001"})], usage=RequestUsage(input_tokens=5, output_tokens=2))
        return ModelResponse(parts=[TextPart("资产正常")], usage=RequestUsage(input_tokens=6, output_tokens=4))

    async def run():
        orchestrator = orchestrator_with(FunctionModel(respond))
        orchestrator.directory = SimpleNamespace(employee=lambda eid, tenant: {"devices": [{"device_id": "DEV-1001", "kind": "laptop", "locked": False}], "tickets": []})
        labels = {"status": "success", "error_code": "none"}
        before = (sample("it_copilot_agent_requests_total", **labels), sample("it_copilot_llm_tokens_total", model="deepseek-chat", direction="input"),
                  sample("it_copilot_agent_tool_calls_total", tool="lookup_asset", error_class="success"))
        await orchestrator._execute(Request("查一下资产 DEV-1001", principal=SimpleNamespace(employee_id="1001", tenant="default")))
        assert sample("it_copilot_agent_requests_total", **labels) == before[0] + 1
        assert sample("it_copilot_llm_tokens_total", model="deepseek-chat", direction="input") == before[1] + 11
        assert sample("it_copilot_agent_tool_calls_total", tool="lookup_asset", error_class="success") == before[2] + 1
    asyncio.run(run())


def test_agent_failure_reason_is_classified():
    async def loop_forever(messages, info):
        return ModelResponse(parts=[ToolCallPart("diagnose", {"code": "vpn"})])

    async def run():
        orchestrator = orchestrator_with(FunctionModel(loop_forever))
        before = sample("it_copilot_agent_failures_total", reason="round_limit")
        response = await orchestrator._execute(Request("VPN 状态"))
        assert response.error_code == "round_limit"
        assert sample("it_copilot_agent_failures_total", reason="round_limit") == before + 1
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
    from agents.intent.recognizer import IntentRecognizer
    monkeypatch.setenv("IT_INTENT_LLM_ENABLED", "false")
    before = (sample("it_copilot_intent_recognitions_total", source="fallback_disabled", intent_group="access"), sample("it_copilot_intent_clarifications_total", intent_group="access"))
    asyncio.run(IntentRecognizer("sk-local").recognize("帮我重置密码"))
    assert sample("it_copilot_intent_recognitions_total", source="fallback_disabled", intent_group="access") == before[0] + 1
    assert sample("it_copilot_intent_clarifications_total", intent_group="access") == before[1] + 1


def test_metrics_endpoint_exposes_new_series():
    from fastapi.testclient import TestClient

    import api.main
    text = TestClient(api.main.app).get("/metrics").text
    for name in ("it_copilot_agent_requests_total", "it_copilot_tool_calls_total", "it_copilot_intent_recognitions_total", "it_copilot_slo_burn_rate"):
        assert name in text


# ── OpenTelemetry ─────────────────────────────────────────────────────────────

def test_tracing_nests_agent_spans_under_chat_without_content():
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from monitor import telemetry

    async def respond(messages, info):
        if not any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            return ModelResponse(parts=[ToolCallPart("diagnose", {"code": "VPN-691"})])
        return ModelResponse(parts=[TextPart("认证失败")])

    async def run():
        orchestrator = orchestrator_with(FunctionModel(respond))
        with telemetry.chat_span("trace-1"):
            await orchestrator._execute(Request("我的员工号 E1001，VPN-691"))

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
    assert any("diagnose" in s.name for s in children)
    assert not any("E1001" in str(value) for s in spans for value in s.attributes.values())
