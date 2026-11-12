"""Exported cases execute through the real application with a deterministic model.

Online samples enter this suite only after review explicitly marks a deterministic
fixture; the online dataset starts empty until real labeled traffic is available.
"""
import asyncio
import json
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart

from evaluation.application import application
from evaluation.fake_model import scripted_model
from evaluation.online_regression import export_cases
from scripts.evaluate_application import trial

ROOT = Path(__file__).resolve().parents[1]


def test_promoted_export_is_readable_and_executable_in_the_application():
    row = {"id": "fixture", "dataset_case_id": "online-fixture", "status": "promoted",
        "snapshot": {"snapshot_partial": False, "turn_record": {"owner": "scope:alice"},
                     "turns": ["工号9999，请说明账号仍在审批"], "response": "等待审批"},
        "label": {"root_cause": "false_claim", "expected": {"outcome": "free", "forbid_text": ["已执行"], "tools": []}}}
    case = export_cases([row])[0]

    async def reply(messages, info):
        return ModelResponse(parts=[TextPart("申请仍在等待审批，请查看审批卡片。")])

    async def run():
        async with application(model=scripted_model(reply)) as (http, state):
            result = await trial(http, state, case, 0)
        assert result["passed"], result
    asyncio.run(run())


ONLINE = json.loads((ROOT / "evaluation/datasets/online_regression.json").read_text())
CASES = [case for case in ONLINE if case.get("offline_fixture") == "plain_answer"]


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_reviewed_plain_answer_regression(case):
    async def reply(messages, info):
        return ModelResponse(parts=[TextPart(case["offline_answer"])])
    async def run():
        async with application(model=scripted_model(reply)) as (http, state):
            result = await trial(http, state, case, 0)
        assert result["passed"], result
    asyncio.run(run())
