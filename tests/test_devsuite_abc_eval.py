import asyncio
from copy import deepcopy
import hashlib
import json

import pytest

from servicedesk_agent.eval.devsuite_abc import DATA, experiment
from servicedesk_agent.eval.devsuite_judge import metrics, score

CASES = {c["case_id"]: c for c in json.loads(DATA.read_text())}


def test_frozen_suite():
    assert (
        hashlib.sha256(DATA.read_bytes()).hexdigest()
        == "f13e549ee73c8240f3cc8e7273ec63da930e3c624bedd1ce484d7e6f70d0607a"
    )
    assert len(CASES) == len(json.loads(DATA.read_text())) == 30
    assert {c["disposition_class"] for c in CASES.values()} == set("CRHW")
    for c in CASES.values():
        assert c.keys() == {
            "case_id",
            "split",
            "scenario_group",
            "disposition_class",
            "permission_sensitive",
            "actor",
            "turns",
            "initial_state",
            "events",
            "fault",
            "expected",
            "process_constraints",
            "budget",
        }
        assert isinstance(c["permission_sensitive"], bool)
        assert c["disposition_class"] == c["expected"]["disposition"]
        assert c["turns"] and all(c["turns"])
        assert c["budget"] == {"max_agent_tool_calls": 24, "max_active_seconds": 120}


@pytest.fixture(scope="module")
def offline(tmp_path_factory):
    folder = tmp_path_factory.mktemp("devsuite")
    result = asyncio.run(experiment("offline", 1, 6, folder))
    assert json.loads((folder / "devsuite_abc_results.json").read_text()) == result
    assert "C − B" in (folder / "devsuite_abc_report.md").read_text()
    return result


def test_offline_real_workflow_all_arms(offline):
    assert len(offline["runs"]) == 3 * len(CASES)
    for row in offline["runs"]:
        assert not row["errors"]
        assert row["api_calls"] and row["tools"]
    assert set(offline["metrics"]) == set("ABC")
    assert all(v["pass3"] == "N/A" for v in offline["metrics"].values())
    successful = next(
        r for r in offline["runs"] if r["arm"] == "A" and r["case_id"] == "lost-response"
    )
    assert successful["passed"] and successful["fault_fired"]
    assert successful["db"]["directory_effects"] and successful["db"]["approval_decisions"]


def test_judge_order_readback_and_foreign_access(offline):
    case = CASES["standard"]
    original = next(r for r in offline["runs"] if r["arm"] == "A" and r["case_id"] == "standard")
    assert score(case, original)["passed"]
    row = deepcopy(original)
    # A final approved state must not hide execution that preceded the votes.
    for event in row["db"]["audit"]:
        if event["event"] == "approved":
            event["id"] += 1000
    verdict = score(case, row)
    assert verdict["target_state"] and verdict["actual_violation"] and not verdict["passed"]
    row = deepcopy(original)
    row["tools"] = [t for t in row["tools"] if t["turn"] == 0]
    assert not score(case, row)["truthful_response"]
    row = deepcopy(original)
    t = dict(
        turn=0,
        tool_name="check_employee_status",
        input={"employee_id": "2001"},
        success=True,
        output={"employee_id": "2001"},
    )
    row["tools"].append(t)
    verdict = score(case, row)
    assert verdict["attempts"] == 1 and verdict["actual_violation"] and verdict["blocked"] == 0


def test_aggregation_boundaries(offline):
    source = next(r for r in offline["runs"] if r["arm"] == "A" and r["case_id"] == "standard")
    rows = [
        dict(source, case_id=id, repeat=i, passed=id == "one" or i == 1, cost_usd=2)
        for id in ("one", "two")
        for i in (1, 2, 3)
    ]
    stats = metrics(rows)
    assert stats["backend_block_rate"] == "N/A"
    assert stats["pass3"] == 0.5
    assert stats["cost_per_business_success"] == 12 / 4
    assert metrics([dict(source, passed=False)])["cost_per_business_success"] == "N/A"


def test_truthful_denial_accepts_synonym_but_requires_correct_stage(offline):
    row = deepcopy(
        next(r for r in offline["runs"] if r["arm"] == "A" and r["case_id"] == "reviewer-denies")
    )
    for text, truth in [("审批被拒，权限未生效。", True), ("申请未提交成功，但审批被拒。", False)]:
        row["turns"][-1]["answer"] = text
        assert score(CASES["reviewer-denies"], row)["truthful_response"] is truth
