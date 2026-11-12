import pytest
import asyncio

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart

from evaluation.attribution import wilson
from scripts.compare_latency_cost import compare
from evaluation.fake_model import scripted_model
from scripts.evaluate_application import isolated_trial


def report(passed, *, cost=1, p99=10, count=1000):
    return {"datasets": [{"dataset": "fixed", "sha256": "frozen", "summary": {"trials": count}, "case_repeats": [["fixed", i] for i in range(count)]}],
        "live": {"sdk_usage": {"prompt": 1000}, "success_rate": passed / count, "success_wilson_95": wilson(passed, count),
                 "agent_latency_ms": {"p99": 1000 * p99}, "pipeline_stage_total_ms": {"p99": 1000 * p99 + 100},
                 "sdk_returned_usage_cost_per_success_usd_peak": cost,
                 "potentially_unreported_model_usage_turns": 0,
                 "routes": {"agent_read": {"turns": count, "round_limit": 0}}},
        "fixed_delay_stream": {str(n): {"summary": {"completed_s": {"p99": p99}}} for n in (1, 4, 8)}}


def test_latency_improvement_cannot_mask_a_material_quality_regression():
    result = compare(report(900), report(500, cost=.5, p99=6))
    assert result["fixed_delay_stream_p99_reduction_at_least_30_percent_all_concurrencies"]
    assert result["real_agent_p99_reduction_at_least_30_percent"]
    assert result["sdk_returned_usage_cost_per_success_nonincreasing"]
    assert not result["success_noninferior"]
    assert result["success_delta_newcombe_wilson_95"][1] < 0


def test_comparison_rejects_a_changed_dataset_or_repetition_count():
    with pytest.raises(ValueError, match="Matched datasets"):
        compare(report(900), report(900, count=1001))
    after = report(900)
    after["datasets"][0]["sha256"] = "changed"
    with pytest.raises(ValueError, match="Matched datasets"):
        compare(report(900), after)


def test_missing_latency_observations_are_unknown_rather_than_zero():
    after = report(900)
    after["live"]["agent_latency_ms"]["p99"] = None
    after["live"]["pipeline_stage_total_ms"]["p99"] = None
    after["live"]["routes"] = {}
    result = compare(report(900), after)
    assert result["real_agent_p99_reduction"] is None
    assert result["real_pipeline_stage_total_p99_reduction_at_least_30_percent"] is None
    assert result["agent_read_round_limit_below_1_percent"] is None


def test_cancelled_model_usage_cannot_claim_a_complete_cost_improvement():
    after = report(900, cost=.5)
    after["live"]["potentially_unreported_model_usage_turns"] = 1
    result = compare(report(900), after)
    assert result["sdk_returned_usage_cost_per_success_nonincreasing"]
    assert result["cost_target_nonincreasing"] is None


def test_repeated_application_cases_get_fresh_business_ledgers():
    async def reply(messages, info):
        if info.output_tools:
            return ModelResponse(parts=[ToolCallPart("classify_it_request", {
                "intent": "permission_request", "confidence": 1, "entities": {"employee_id": ["1001"], "system": ["gitlab"], "role": ["developer"]},
                "requested_actions": ["request_access"]})])
        if not any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts):
            return ModelResponse(parts=[ToolCallPart("request_access", {
                "employee_id": "1001", "system": "GitLab", "role": "developer", "reason": "代码审阅", "duration_hours": 24})])
        return ModelResponse(parts=[TextPart("已生成权限申请草稿，请确认提交。")])

    async def run():
        case = {"id": "isolated-grant", "turns": ["工号1001，申请GitLab developer权限24小时，代码审阅"],
                "outcome": "grant", "system": "gitlab", "role": "developer"}
        for repeat in range(2):
            row, _, _, _ = await isolated_trial(case, repeat, model=scripted_model(reply))
            assert row["passed"]
            assert row["business_before"]["access_ledger"] == []
            assert len(row["business_after"]["access_ledger"]) == 1
    asyncio.run(run())


@pytest.mark.parametrize("concurrencies", [(), (1,), (1, 4)])
def test_stream_gate_requires_all_planned_concurrencies(concurrencies):
    before, after = report(900), report(900, p99=6)
    before["fixed_delay_stream"] = {str(n): before["fixed_delay_stream"][str(n)] for n in concurrencies}
    after["fixed_delay_stream"] = {str(n): after["fixed_delay_stream"][str(n)] for n in concurrencies}
    assert compare(before, after)["fixed_delay_stream_p99_reduction_at_least_30_percent_all_concurrencies"] is None


def test_stream_comparison_rejects_unmatched_concurrencies():
    after = report(900)
    del after["fixed_delay_stream"]["8"]
    with pytest.raises(ValueError, match="Matched stream"):
        compare(report(900), after)


def test_performance_comparison_rejects_an_unmeasured_model_baseline():
    before = report(900)
    before["live"]["sdk_usage"]["prompt"] = 0
    with pytest.raises(ValueError, match="measured model token usage"):
        compare(before, report(900))


def test_live_application_cli_rejects_missing_credentials_before_startup():
    import os
    from pathlib import Path
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, "scripts/evaluate_application.py", "--limit", "1"],
        cwd=root, env={**os.environ, "OPENAI_API_KEY": ""}, capture_output=True, text=True)
    assert result.returncode == 2
    assert "OPENAI_API_KEY is required" in result.stderr
    assert "IT-Copilot ready" not in result.stderr
