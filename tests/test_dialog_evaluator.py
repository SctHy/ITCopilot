"""EndToEndEvaluator / LLMJudge with FunctionModel stand-ins: scoring, deterministic checks, judge failure, baseline."""
import asyncio
import json

from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from agents.agent_orchestrator import AgentOrchestrator
from evaluation.evaluator import DEFAULT_DIALOG_CASES, EndToEndEvaluator


def agent_model():
    async def respond(messages, info):
        if info.output_tools:
            return ModelResponse(parts=[])  # 意图识别走规则兜底即可
        return ModelResponse(parts=[TextPart("请提供错误码，我会先做只读诊断，必要时创建工单。")])
    return FunctionModel(respond)


def judge_model(scores):
    async def respond(messages, info):
        return ModelResponse(parts=[TextPart(scores if isinstance(scores, str) else json.dumps(scores))])
    return FunctionModel(respond)


def make_evaluator(tmp_path, judge_scores):
    orchestrator = AgentOrchestrator("sk-local")
    model = agent_model()
    orchestrator._intent_recognizer.llm = model
    for agents in orchestrator._pool.values():
        for agent in agents:
            agent._llm = model
    evaluator = EndToEndEvaluator(orchestrator, orchestrator._intent_recognizer, "sk-local", baseline_path=str(tmp_path / "baseline.json"))
    evaluator._judge._llm = judge_model(judge_scores)
    return evaluator


def test_dialog_case_is_scored_checked_and_saved_as_baseline(tmp_path, monkeypatch):
    monkeypatch.setenv("IT_INTENT_LLM_ENABLED", "false")
    evaluator = make_evaluator(tmp_path, {"relevance": .9, "accuracy": .9, "completeness": .8, "helpfulness": .9})
    report = asyncio.run(evaluator.run(dialog_cases=DEFAULT_DIALOG_CASES[:1]))
    assert report.status == "complete" and report.passed == report.total == 1
    result = report.results[0]
    assert result.metadata["deterministic_checks"] == {"agent": True}       # 路由到 network_software
    assert report.avg_scores["relevance"] == .9
    assert (tmp_path / "baseline.json").exists()


def test_judge_failure_is_excluded_and_does_not_update_baseline(tmp_path, monkeypatch):
    monkeypatch.setenv("IT_INTENT_LLM_ENABLED", "false")
    evaluator = make_evaluator(tmp_path, "我觉得回答还不错")                  # 没有 JSON 评分
    report = asyncio.run(evaluator.run(dialog_cases=DEFAULT_DIALOG_CASES[:1]))
    assert report.status == "failed" and report.evaluation_errors == 1
    assert report.results[0].metadata["judge_failed"] is True
    assert not (tmp_path / "baseline.json").exists()


def test_regression_against_previous_baseline_is_detected(tmp_path, monkeypatch):
    monkeypatch.setenv("IT_INTENT_LLM_ENABLED", "false")
    good = {"relevance": .95, "accuracy": .95, "completeness": .95, "helpfulness": .95}
    asyncio.run(make_evaluator(tmp_path, good).run(dialog_cases=DEFAULT_DIALOG_CASES[:1]))
    worse = make_evaluator(tmp_path, {"relevance": .5, "accuracy": .5, "completeness": .5, "helpfulness": .5})
    report = asyncio.run(worse.run(dialog_cases=DEFAULT_DIALOG_CASES[:1]))
    assert report.regressions and "回归" in report.regression_note
