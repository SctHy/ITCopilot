"""路由的选择、合规、健康与流式边界，使用真实 FunctionModel 和数据库。"""
import asyncio
import copy
from dataclasses import replace
import time

import pytest
import yaml
from pydantic import ValidationError
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from agents.base_agent import BaseAgent
from agents.types import Request
from core.model_router import DEFAULT_CONFIG, ModelRouter, RoutingConfig
from core.tenancy import TenantRegistry
from core.tenant_context import tenant_scope
from evaluation.fake_model import scripted_model
from tests.test_workflow import ALICE
from tooling.tool_spec import make_tool
from workflow.store import WorkflowStore


async def okay(messages, info):
    return ModelResponse(parts=[TextPart("查询完成")])


def configured(tmp_path, *, config=None, models=None, engine=None):
    config = copy.deepcopy(yaml.safe_load(DEFAULT_CONFIG.read_text())) if config is None else config
    config["models"]["backup"] = copy.deepcopy(config["models"]["deepseek-flash"])
    for route in config["routes"].values():
        route["candidates"] = ["deepseek-flash", "backup"]
    path = tmp_path / "routes.yaml"
    path.write_text(yaml.safe_dump(config))
    return ModelRouter(path, engine=engine, models={"deepseek-flash": FunctionModel(okay), "backup": FunctionModel(okay)} if models is None else models)


def test_invalid_config_fails_at_load_and_confidential_needs_candidate(tmp_path):
    config = yaml.safe_load(DEFAULT_CONFIG.read_text())
    config["routes"]["agent_write"]["candidates"] = ["missing"]
    with pytest.raises(ValidationError, match="unknown model"):
        RoutingConfig.model_validate(config)
    config = yaml.safe_load(DEFAULT_CONFIG.read_text())
    config["models"]["deepseek-flash"]["supports"] = ["tools"]
    with pytest.raises(ValidationError, match="missing"):
        RoutingConfig.model_validate(config)
    config = yaml.safe_load(DEFAULT_CONFIG.read_text())
    config["tenants"]["regulated"] = {"data_class": "confidential", "regions": ["cn"]}
    with pytest.raises(ValidationError, match="no compliant"):
        RoutingConfig.model_validate(config)


def test_compliance_filters_before_preference(tmp_path):
    router = configured(tmp_path)
    data = router.config.model_dump()
    data["models"]["backup"]["data_classes"].append("confidential")
    data["tenants"]["regulated"] = {"data_class": "confidential", "regions": ["cn"]}
    router.path.write_text(yaml.safe_dump(data))
    assert router.pick("agent_read", "regulated").name == "backup"


def test_circuit_and_latency_change_selection(tmp_path):
    router = configured(tmp_path)
    first = router.pick("agent_read", "default")
    for _ in range(5):
        router.record(first, 1, success=False)
    assert router.pick("agent_read", "default").name == "backup"
    router.breakers[first.name].opened_at -= 61
    probe = router.pick("agent_read", "default")
    router.record(probe, 1, success=True)
    router.latencies[first.name].clear()
    router.latencies[first.name].append((time.monotonic(), 12))
    selected = router.pick("agent_read", "default")
    assert selected.name == "backup" and selected.reason == "latency_shift"


def test_cost_does_not_double_count_cached_input(tmp_path):
    router = configured(tmp_path)
    assert router.cost("deepseek-flash", {"input_tokens": 1_000_000, "cache_read_tokens": 800_000,
        "output_tokens": 100_000}) == pytest.approx(.2 * .3 + .8 * .006 + .1 * 1.2)


def test_failover_before_output(tmp_path):
    calls = []
    async def unavailable(messages, info):
        calls.append("primary")
        raise TimeoutError("provider timeout")
    async def available(messages, info):
        calls.append("backup")
        return ModelResponse(parts=[TextPart("查询完成")])
    router = configured(tmp_path, models={"deepseek-flash": FunctionModel(unavailable), "backup": FunctionModel(available)})
    agent = BaseAgent(None, "", router=router)
    result = asyncio.run(agent.handle(Request("查询", allowed_writes=frozenset())))
    assert result.success and calls == ["primary", "backup"]
    assert result.observability["model"] == "backup" and result.observability["route_reason"] == "failover"


def test_no_failover_after_answer_delta(tmp_path):
    calls, events = [], []
    async def broken_stream(messages, info):
        calls.append("primary")
        yield "已经向用户推送的回答文字需要至少二十四个字符才能触发flush"
        raise TimeoutError("disconnected after output")
    async def backup(messages, info):
        calls.append("backup")
        return ModelResponse(parts=[TextPart("替补")])
    async def emit(event, data):
        events.append(event)
    router = configured(tmp_path, models={"deepseek-flash": FunctionModel(stream_function=broken_stream), "backup": scripted_model(backup)})
    result = asyncio.run(BaseAgent(None, "", router=router).handle(Request("查询", emit=emit, stream_answer=True)))
    assert not result.success and "answer_delta" in events and calls == ["primary"]


def test_budget_hides_write_tools_but_allows_reads(tmp_path, db_url):
    store = WorkflowStore(db_url)
    TenantRegistry(store.engine).create("budget", actor="system", quotas={"daily_cost_usd": 0})
    seen = []
    async def respond(messages, info):
        seen.append({tool.name for tool in info.function_tools})
        return ModelResponse(parts=[TextPart("当日预算已用完，不能起草申请")])
    router = configured(tmp_path, engine=store.engine,
        models={"deepseek-flash": FunctionModel(respond), "backup": FunctionModel(respond)})
    agent = BaseAgent(None, "", router=router)
    agent.set_shared_tools({"create_ticket": make_tool("create_ticket", "写入", {}, lambda req, args: {}, retry_safe=False),
                           "diagnose": make_tool("diagnose", "诊断", {}, lambda req, args: {})})
    async def run():
        with tenant_scope("budget"):
            return await agent.handle(Request("申请工单", principal=replace(ALICE, tenant="budget")))
    result = asyncio.run(run())
    assert result.success and seen == [{"diagnose"}]
    assert result.observability["route_reason"] == "budget"
    assert router.pick("agent_read", "default").reason != "budget"


def test_invalid_hot_reload_raises_and_valid_reload_records_change(tmp_path, db_url):
    from core.db import change_events
    from sqlalchemy import select
    store = WorkflowStore(db_url)
    router = configured(tmp_path, engine=store.engine)
    data = router.config.model_dump()
    data["routes"]["agent_read"]["max_rounds"] = 0
    router.path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValidationError):
        router.pick("agent_read", "default")
    data["routes"]["agent_read"]["max_rounds"] = 4
    router.path.write_text(yaml.safe_dump(data))
    assert router.pick("agent_read", "default").max_rounds == 4
    with store.engine.connect() as db:
        assert db.execute(select(change_events.c.kind)).scalar_one() == "model_route"


def test_thinking_is_route_config_not_an_environment_toggle(tmp_path):
    router = configured(tmp_path)
    route = router.pick("agent_write", "default")
    assert route.settings["extra_body"] == {"thinking": {"type": "disabled"}}
    assert route.max_rounds == 5
    data = router.config.model_dump()
    data["routes"]["agent_write"]["thinking"] = "max"
    router.path.write_text(yaml.safe_dump(data))
    settings = router.pick("agent_write", "default").settings
    assert settings["max_tokens"] == 16000 and settings["extra_body"]["reasoning_effort"] == "max"
    assert "temperature" not in settings


def test_failed_turn_keeps_known_tokens_and_cost():
    from pydantic_ai.messages import ToolCallPart
    from pydantic_ai.usage import RequestUsage
    calls = []
    async def respond(messages, info):
        calls.append(1)
        if len(calls) == 1:
            return ModelResponse(parts=[ToolCallPart("diagnose", {})],
                usage=RequestUsage(input_tokens=1000, output_tokens=20))
        raise TimeoutError("provider failed after a tool result")
    router = ModelRouter(models={"deepseek-flash": FunctionModel(respond)})
    agent = BaseAgent(None, "", router=router)
    agent.set_shared_tools({"diagnose": make_tool("diagnose", "诊断", {}, lambda req, args: {"ok": True})})
    result = asyncio.run(agent.handle(Request("检查VPN", allowed_writes=frozenset())))
    assert not result.success and result.error_code == "model_unavailable"
    assert result.token_usage["input_tokens"] == 1000 and result.token_usage["output_tokens"] == 20
    assert result.observability["cost_usd"] == pytest.approx(router.cost("deepseek-flash", result.token_usage))
