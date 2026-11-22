import asyncio
from pathlib import Path

from pydantic_ai.messages import ModelResponse, TextPart

from tests.support.application import application
from tests.support.fake_model import scripted_model
from tests.support.evaluate_application import trial

ROOT = Path(__file__).resolve().parents[1]


def test_pending_approval_answer_on_selected_agent(release_agent_spec):
    case = {"id": "pending-approval", "outcome": "free", "turns": ["工号1001，请说明账号仍在审批"],
            "forbid_text": ["已执行"], "requester_employee_id": "1001"}

    async def reply(messages, info):
        return ModelResponse(parts=[TextPart("申请仍在等待审批，请查看审批卡片。")])

    async def run():
        async with application(model=scripted_model(reply), agent_spec=release_agent_spec) as (http, state):
            result = await trial(http, state, case, 0)
        assert result["passed"], result
        assert_target(result, release_agent_spec)
    asyncio.run(run())


def assert_target(result, target):
    if target is not None:
        for turn in result["turns"]:
            version = turn["trace"]["trace"]["observability"]["versions"]
            assert (version["agent_spec"], version["agent_version"]) == target
