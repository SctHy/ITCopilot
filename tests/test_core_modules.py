import asyncio
import os
import tempfile

from agents.agent_orchestrator import AgentOrchestrator, AgentType
from core.intent_recognizer import IntentCategory, IntentRecognizer
from tooling.tool_manager import MCPToolManager, Tool
from tooling.knowledge_base import KnowledgeBase
from core.planner import build_plan, is_complex_request
from core.intent_calibration import calibrate, ece, brier, fit_temperatures


def test_tool_manager_schema_cache_and_fallback():
    async def run():
        calls = []
        async def handler(params, context):
            calls.append(params["q"])
            return {"answer": params["q"]}
        manager = MCPToolManager("sk-local")
        manager.register(Tool("lookup", "lookup", handler, {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}, cache_ttl=60))
        invalid = await manager.call("lookup", {})
        assert not invalid.success and "Schema" in invalid.error
        first = await manager.call("lookup", {"q": "vpn"})
        second = await manager.call("lookup", {"q": "vpn"})
        assert first.success and second.cached and calls == ["vpn"]
    asyncio.run(run())


def test_tool_manager_timeout_uses_fallback():
    async def run():
        async def slow(params, context):
            await asyncio.sleep(.05)
        manager = MCPToolManager("sk-local")
        manager.register(Tool("slow", "slow", slow, {"type": "object"}, timeout_s=.001, fallback=lambda p, c, e: {"fallback": True}))
        result = await manager.call("slow", {})
        assert not result.success and result.data["fallback"] is True
        assert manager._tools["slow"].stats.failed == 1
    asyncio.run(run())


def test_orchestrator_routes_core_intents():
    orchestrator = AgentOrchestrator(api_key="sk-local")
    assert orchestrator._route(IntentCategory.VPN_ISSUE) == AgentType.NETWORK_SOFTWARE
    assert orchestrator._route(IntentCategory.PERMISSION_REQUEST) == AgentType.ACCESS
    assert orchestrator._route(IntentCategory.TICKET_STATUS) == AgentType.TICKET


def test_intent_metrics_expose_disabled_fallback():
    async def run():
        old = os.environ.get("IT_INTENT_LLM_ENABLED")
        os.environ["IT_INTENT_LLM_ENABLED"] = "false"
        try:
            recognizer = IntentRecognizer("sk-local")
            result = await recognizer.recognize("你是谁")
            assert result.recognition_source == "fallback_disabled"
            assert recognizer.metrics()["fallback_disabled"] == 1
        finally:
            if old is None:
                os.environ.pop("IT_INTENT_LLM_ENABLED", None)
            else:
                os.environ["IT_INTENT_LLM_ENABLED"] = old
    asyncio.run(run())


def test_rag_hybrid_returns_citations_and_department_scope():
    os.environ["IT_COPILOT_SEED_DEMO_DOCS"] = "false"
    with tempfile.TemporaryDirectory() as path:
        kb = KnowledgeBase(chroma_path=path)
        kb.add_documents([
            {"title": "研发 VPN SOP", "content": "研发 VPN DNS 排障和客户端版本检查", "department": "研发", "source": "sop/vpn.md"},
            {"title": "财务 ERP SOP", "content": "财务 ERP 权限申请和审批", "department": "财务", "source": "sop/erp.md"},
        ])
        rows = kb.search("VPN DNS 排障", top_k=2, department="研发", mode="hybrid")
        assert rows and rows[0]["title"] == "研发 VPN SOP"
        assert rows[0]["citation"].startswith("研发 VPN SOP")
        assert all(row["department"] in {"研发", "*"} for row in rows)
    os.environ.pop("IT_COPILOT_SEED_DEMO_DOCS", None)


def test_phase5_planner_is_bounded_and_parallel_ready():
    message = "请申请 GitLab 权限并准备一台新电脑"
    assert is_complex_request(message)
    plan = build_plan(message, "req-1")
    assert len(plan.steps) <= plan.max_steps
    assert {step.agent_type for step in plan.steps} >= {"access", "endpoint"}
    assert all(step.status == "pending" for step in plan.steps)


def test_planner_keywords_are_case_insensitive():
    upper = build_plan("配置 VPN", "upper")
    lower = build_plan("配置 vpn", "lower")
    assert [step.agent_type for step in upper.steps] == [step.agent_type for step in lower.steps]
    assert any(step.agent_type == "network_software" for step in upper.steps)


def test_temperature_calibration_is_bounded_and_improves_overconfident_rows():
    rows = {"network": [(.95, True), (.95, False), (.8, True), (.8, False)]}
    temperatures = fit_temperatures(rows)
    assert temperatures["network"] > 0
    calibrated = [(calibrate(p, temperatures["network"]), ok) for p, ok in rows["network"]]
    assert all(0 < p < 1 for p, _ in calibrated)
    assert ece(calibrated) >= 0 and brier(calibrated) >= 0


def test_phase5_approval_state_machine():
    async def run():
        import fakeredis.aioredis
        from memory.conversation_memory import MemoryManager
        with tempfile.TemporaryDirectory() as path:
            manager = MemoryManager(redis_url="redis://127.0.0.1:6399/0", chroma_host="127.0.0.1", chroma_port=8999, chroma_path=path, api_key="sk-local")
            manager._redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
            await manager.create_approval("a1", {"actions": ["request_access"], "risk": "high"})
            assert (await manager.get_approval("a1"))["status"] == "pending"
            approved = await manager.decide_approval("a1", "approved", "tester")
            assert approved["execution"] == "mock_executed"
            assert (await manager.decide_approval("a1", "rejected", "tester"))["status"] == "approved"
            await manager.close()
    asyncio.run(run())
