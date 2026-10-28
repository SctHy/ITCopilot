"""Generalist baseline wiring and the paired statistics used to compare strategies."""
import asyncio
import importlib.util
import pathlib

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel


def _module():
    path = pathlib.Path(__file__).resolve().parent.parent / "scripts/compare_strategies.py"
    spec = importlib.util.spec_from_file_location("compare_strategies", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generalist_uses_one_agent_with_tools_from_every_domain():
    from agents.agent_orchestrator import AgentOrchestrator, AgentType, Request
    from core.intent_recognizer import IntentCategory

    calls = [("lookup_error_code", {"error_code": "VPN-691"}), ("lookup_asset", {"asset_id": "DEV-1"})]

    async def reply(messages, info):
        done = sum(isinstance(p, ToolReturnPart) for m in messages for p in m.parts)
        return ModelResponse(parts=[ToolCallPart(*calls[done])] if done < len(calls) else [TextPart("两项均已检查")])

    async def run():
        orchestrator = AgentOrchestrator("test")
        orchestrator.planning_mode = "generalist"
        orchestrator._generalist._llm = FunctionModel(reply)
        result = await orchestrator.run(Request("VPN-691 且 DEV-1 黑屏", intent=IntentCategory.VPN_ISSUE, intent_group="network"))
        assert result.agent_type == AgentType.GENERALIST and result.tools_used == ["lookup_error_code", "lookup_asset"]
        assert orchestrator.planning_mode == "generalist"  # default strategy
        assert orchestrator._route(IntentCategory.VPN_ISSUE) != AgentType.GENERALIST  # intent routing never selects it

    asyncio.run(run())


def test_mcnemar_and_wilson():
    module = _module()
    assert module.mcnemar_exact(0, 0) == 1.0
    assert module.mcnemar_exact(0, 6) == 0.0312       # 2 * 0.5^6
    assert module.mcnemar_exact(3, 3) == 1.0
    low, high = module.wilson(8, 10)
    assert (low, high) == (0.49, 0.943)                 # Wilson(8/10) = 0.4902–0.9433


def test_paired_analysis_counts_discordant_cases_strictly_across_repeats():
    module = _module()
    def trial(case, mode, passed, repeat=0):
        return {"case_id": case, "stratum": "C2", "planning": mode, "passed": passed, "repeat": repeat, "checks": {},
                "agent_tokens": 10, "latency_s": 1.0}
    report = {"summary": {"single": {}, "bounded": {}}, "trials": [
        trial("a", "single", False), trial("a", "bounded", True),
        trial("b", "single", True), trial("b", "bounded", True, 0), trial("b", "bounded", False, 1), trial("b", "single", True, 1),
    ]}
    result = module.analyse(report)
    pair = result["paired"]["single_vs_bounded"]["ALL"]
    assert pair["only_bounded"] == ["a"] and pair["only_single"] == ["b"]   # b fails one bounded repeat -> strict fail
    assert result["strata"]["C2"]["bounded"]["trial_rate"] == round(2 / 3, 3)


def test_bounded_all_gives_specialists_every_tool_and_bounded_does_not():
    from agents.agent_orchestrator import AgentOrchestrator, AgentType

    orchestrator = AgentOrchestrator("test")
    orchestrator.planning_mode = "bounded_all"
    assert "lookup_asset" in orchestrator._pool[AgentType.NETWORK_SOFTWARE][0].get_tools()
    assert "lookup_asset" not in orchestrator._pool[AgentType.TRIAGE][0].get_tools()
    orchestrator.planning_mode = "bounded"
    assert "lookup_asset" not in orchestrator._pool[AgentType.NETWORK_SOFTWARE][0].get_tools()


def test_llm_planner_routes_by_content_and_drops_unrequested_ticket():
    from core.planner import LLMPlanner

    async def reply(messages, info):
        return ModelResponse(parts=[ToolCallPart("make_plan", {"steps": [
            {"step_id": "network", "agent_type": "network_software", "title": "解释 403"},
            {"step_id": "ticket", "agent_type": "ticket", "title": "建单"}]})])

    plan = asyncio.run(LLMPlanner(FunctionModel(reply)).plan("系统返回 403 是什么意思？", "p"))
    assert [s.agent_type for s in plan.steps] == ["network_software"]   # ticket removed: user did not ask


def test_llm_planner_falls_back_to_rules_on_failure():
    from core.planner import LLMPlanner

    async def broken(messages, info):
        raise RuntimeError("model down")

    planner = LLMPlanner(FunctionModel(broken))
    plan = asyncio.run(planner.plan("电脑 DEV-1 黑屏，同时 VPN 报 VPN-691", "p"))
    assert [s.agent_type for s in plan.steps] == ["endpoint", "network_software"] and planner.metrics["fallback_plans"] == 1


def test_merge_keeps_pairs_when_dropping_errored_or_off_script_cases(tmp_path):
    import json
    module = _module()
    def trial(case, mode, passed, **extra):
        return {"case_id": case, "stratum": "T1", "planning": mode, "passed": passed, "checks": {}, "agent_tokens": 1, "latency_s": 1.0, "turns": 2, **extra}
    files = []
    for mode, rows in {"single": [trial("a", "single", True), trial("b", "single", False, error_type="ModelQuotaExceededError")],
                       "bounded": [trial("a", "bounded", True, simulator_flags=["asked_unlisted_ticket"]), trial("b", "bounded", True)]}.items():
        path = tmp_path / f"sim_{mode}_run1.json"
        path.write_text(json.dumps({"summary": {mode: {}}, "trials": rows}))
        files.append(path)
    assert {t["case_id"] for t in module.merge(files, exclude_errored=True)["trials"]} == {"a"}
    assert {t["case_id"] for t in module.merge(files, exclude_flagged=True)["trials"]} == {"b"}
    assert module.merge(files)["trials"][0]["repeat"] == 1
