"""资格评测保留确定性oracle；报告不足或跨题集比较必须失败。"""
import asyncio
import json

from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from core.model_router import ModelRouter
from evaluation.route_qualification import PROBES, probe_passed, qualify
from scripts.check_model_routes import check


def test_summary_qualification_records_actual_calls_and_failures():
    async def reply(messages, info):
        return ModelResponse(parts=[TextPart("泛泛而谈，没有要求保留的业务标识")])
    router = ModelRouter(models={"deepseek-flash": FunctionModel(reply)})
    report = asyncio.run(qualify(router, "summary", "deepseek-flash", 3))
    assert report["trials"] == 12 and report["passed"] == 0 and report["route_calls"] == 12
    assert report["cost_per_success_usd"] is None


def test_rewrite_and_ranking_oracles_reject_missing_evidence():
    rewrite, ranking = PROBES["rewrite_rerank"][0], PROBES["rewrite_rerank"][2]
    assert not probe_passed("rewrite_rerank", rewrite, ["VPN", "忽略错误码"])
    assert probe_passed("rewrite_rerank", rewrite, ["VPN错误691", "如何检查账号锁定"])
    assert not probe_passed("rewrite_rerank", ranking, [0, 1, 2])
    assert not probe_passed("rewrite_rerank", ranking, [1, 1, 0])
    assert probe_passed("rewrite_rerank", ranking, [1, 0, 2])


def test_route_gate_rejects_missing_reports_and_unexercised_roles(tmp_path):
    errors = check(reports=tmp_path)
    assert len(errors) == 7 and all("missing report" in error for error in errors)
    path = tmp_path / "agent_read__deepseek-flash.json"
    path.write_text(json.dumps({"route": "agent_read", "model": "deepseek-flash", "status": "complete",
        "repeats": 3, "trials": 10, "passed": 10, "kind": "real_model", "route_calls": 0}))
    errors = check(reports=tmp_path)
    assert any("incomplete or non-live" in error for error in errors)


def test_application_evidence_reuse_keeps_failed_cases_and_checks_model(tmp_path):
    import pytest
    from scripts.evaluate_model_route import from_application_reports
    path = tmp_path / "application.json"
    turns = [{"trace": {"trace": {"observability": {"versions": {
        "route": "agent_read", "model": "deepseek-flash", "cost_usd": .01}}}}}]
    report = {"status": "complete", "dataset_sha256": "frozen-dataset", "code_sha256": "frozen-code",
        "trials": [{"case_id": "one", "repeat": repeat, "passed": repeat != 1,
                    "turns": turns, "latency_s": 1 + repeat} for repeat in range(3)]}
    path.write_text(json.dumps(report))
    measured = from_application_reports([path], "agent_read", "deepseek-flash", ModelRouter(), 3)
    assert measured["trials"] == 3 and measured["passed"] == 2 and measured["route_calls"] == 3
    report["trials"][0]["turns"][0]["trace"]["trace"]["observability"]["versions"]["model"] = "other"
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError, match="instead of"):
        from_application_reports([path], "agent_read", "deepseek-flash", ModelRouter(), 3)
