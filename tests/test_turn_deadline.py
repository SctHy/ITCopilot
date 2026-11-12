import asyncio
import time

import pytest
import yaml
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from agents.base_agent import BaseAgent
from agents.types import Request
from core.model_router import DEFAULT_CONFIG, ModelRouter
from evaluation.application import application
from evaluation.fake_model import scripted_model
from tests.test_workflow import ALICE


def router_for(tmp_path, model, *, deadline=.05, request_timeout=1):
    config = yaml.safe_load(DEFAULT_CONFIG.read_text())
    for name in ("agent_read", "agent_write"):
        config["routes"][name].update(turn_deadline_s=deadline, request_timeout_s=request_timeout)
    path = tmp_path / "routes.yaml"
    path.write_text(yaml.safe_dump(config))
    return ModelRouter(path, models={"deepseek-flash": model})


@pytest.mark.parametrize("field,value", [("turn_deadline_s", float("inf")), ("turn_deadline_s", 0),
    ("request_timeout_s", float("nan")), ("request_timeout_s", -1)])
def test_invalid_deadline_configuration_refuses_startup(tmp_path, field, value):
    config = yaml.safe_load(DEFAULT_CONFIG.read_text())
    config["routes"]["agent_read"][field] = value
    path = tmp_path / "invalid.yaml"
    path.write_text(yaml.safe_dump(config))
    with pytest.raises(ValueError):
        ModelRouter(path, models={"deepseek-flash": FunctionModel(lambda messages, info: ModelResponse(parts=[TextPart("unused")]))})


def test_turn_deadline_returns_partial_without_retrying_or_claiming_a_write(tmp_path):
    calls = []
    async def slow(messages, info):
        calls.append(time.monotonic())
        await asyncio.sleep(1)
        return ModelResponse(parts=[TextPart("too late")])
    router = router_for(tmp_path, FunctionModel(slow))
    req = Request("读取权限", principal=ALICE, allowed_writes=frozenset())
    started = time.monotonic()
    response = asyncio.run(BaseAgent(None, "test", router=router).handle(req))
    assert time.monotonic() - started < .5
    assert response.status == "partial" and response.error_code == "deadline"
    assert response.content == "处理超时，请重试"
    assert req.signals.count("deadline") == 1 and len(calls) == 1
    assert response.tools_used == [] and response.tool_traces == []


def test_each_model_request_gets_the_remaining_budget(tmp_path):
    from tooling.tool_spec import make_tool

    timeouts = []
    async def call(messages, info):
        timeouts.append(info.model_settings["timeout"])
        if len(timeouts) == 1:
            await asyncio.sleep(.04)
            return ModelResponse(parts=[ToolCallPart("lookup", {})])
        return ModelResponse(parts=[TextPart("查询完成")])
    router = router_for(tmp_path, FunctionModel(call), deadline=.2, request_timeout=.18)
    agent = BaseAgent(None, "test", router=router)
    agent.set_shared_tools({"lookup": make_tool("lookup", "查台账", {}, lambda req, args: {"ok": True})})
    result = asyncio.run(agent.handle(Request("查询", principal=ALICE, allowed_writes=frozenset())))
    assert result.status == "success"
    assert len(timeouts) == 2 and timeouts[0] <= .18 and 0 < timeouts[1] < timeouts[0]


def test_request_timeout_expires_before_the_longer_turn_deadline(tmp_path):
    async def slow(messages, info):
        await asyncio.sleep(.3)
        return ModelResponse(parts=[TextPart("too late")])
    router = router_for(tmp_path, FunctionModel(slow), deadline=1, request_timeout=.03)
    started = time.monotonic()
    result = asyncio.run(BaseAgent(None, "test", router=router).handle(
        Request("查询", principal=ALICE, allowed_writes=frozenset())))
    assert time.monotonic() - started < .2
    assert result.status == "failed" and result.error_code == "model_unavailable"


def test_deadline_keeps_a_real_draft_and_its_turn_record(tmp_path):
    async def reply(messages, info):
        if info.output_tools:
            return ModelResponse(parts=[ToolCallPart("classify_it_request", {
                "intent": "permission_request", "confidence": 1, "entities": {"employee_id": ["1001"], "system": ["gitlab"], "role": ["readonly"]},
                "requested_actions": ["request_access"]})])
        if not any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts):
            return ModelResponse(parts=[ToolCallPart("request_access", {
                "employee_id": "1001", "system": "GitLab", "role": "readonly", "reason": "代码审阅", "duration_hours": 24})])
        await asyncio.sleep(1)
        return ModelResponse(parts=[TextPart("too late")])

    async def scenario():
        from sqlalchemy import select
        from core.db import read, turn_records

        async with application(model=scripted_model(reply)) as (http, state):
            for route in ("agent_read", "agent_write"):
                state.orchestrator.router.config.routes[route] = state.orchestrator.router.config.routes[route].model_copy(update={"turn_deadline_s": .1})
            response = await http.post("/chat", json={"message": "工号1001，申请gitlab readonly权限24小时，代码审阅", "conv_id": "deadline"})
            assert response.status_code == 200
            body = response.json()
            assert body["status"] == "partial" and body["error_code"] == "deadline"
            assert body["approval_id"] and "草稿" in body["response"]
            approval = (await http.get("/approvals/" + body["approval_id"])).json()
            assert approval["status"] == "draft" and approval["execution"] == "not_executed"
            assert state.workflow.employee("1001", "default")["grants"] == []
            with read(state.workflow.engine) as db:
                record = db.execute(select(turn_records).where(turn_records.c.request_id == body["request_id"])).mappings().one()
            assert "deadline" in record["signals"] and record["approval_id"] == body["approval_id"]
    asyncio.run(scenario())


def test_claim_correction_time_is_measured_inside_the_turn_deadline(tmp_path):
    calls = []
    async def claim(messages, info):
        calls.append(1)
        if len(calls) == 2:
            await asyncio.sleep(.01)
            return ModelResponse(parts=[TextPart("未提交申请")])
        return ModelResponse(parts=[TextPart("权限申请已提交")])
    router = router_for(tmp_path, FunctionModel(claim), deadline=1)
    req = Request("申请权限", principal=ALICE, allowed_writes=frozenset({"request_access"}))
    result = asyncio.run(BaseAgent(None, "test", router=router).handle(req))
    assert result.status == "success" and len(calls) == 2
    assert "claim_corrected" in req.signals and req.stage_ms["claim_correction"] >= 10


def test_streamed_prefix_is_reset_when_the_turn_deadline_expires(tmp_path):
    events = []
    async def reply(messages, info):
        return ModelResponse(parts=[TextPart("unused")])
    async def stream(messages, info):
        yield "已经开始生成的一段诊断说明，但后续模型输出没有在截止时间内结束。"
        await asyncio.sleep(1)
        yield "too late"
    async def emit(event, payload):
        events.append((event, payload))
    router = router_for(tmp_path, FunctionModel(reply, stream_function=stream))
    req = Request("查询", principal=ALICE, allowed_writes=frozenset(), stream_answer=True, emit=emit)
    response = asyncio.run(BaseAgent(None, "test", router=router).handle(req))
    assert response.error_code == "deadline" and req.first_token_ms is not None
    assert events[0][0] == "answer_delta" and events[-1][0] == "answer_reset"


def test_timeout_at_deadline_releases_half_open_model_probe(tmp_path):
    from core.deadline_model import TurnDeadlineExceeded
    from tooling.tool_manager import CircuitState

    async def timeout_before_timer_runs(messages, info):
        # Exhausting the budget before the next request raises without cancellation.
        raise TurnDeadlineExceeded("Agent turn deadline exceeded")

    router = router_for(tmp_path, FunctionModel(timeout_before_timer_runs), deadline=.01)
    breaker = router.breakers["deepseek-flash"]
    breaker.threshold = 1
    breaker.record_failure()
    breaker.opened_at -= breaker.recovery_s
    result = asyncio.run(BaseAgent(None, "test", router=router).handle(
        Request("查询", principal=ALICE, allowed_writes=frozenset())))
    assert result.error_code == "deadline"
    assert breaker.state == CircuitState.OPEN
    breaker.opened_at -= breaker.recovery_s
    assert breaker.allow()
