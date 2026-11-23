"""路由的选择、合规、健康与流式边界，使用真实 FunctionModel 和数据库。"""
import asyncio
import copy
from dataclasses import replace

import pytest
import yaml
from pydantic import ValidationError
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from servicedesk_agent.agent.base_agent import BaseAgent
from servicedesk_agent.agent.types import Request
from servicedesk_agent.agent.model_router import DEFAULT_CONFIG, ModelRouter, RoutingConfig
from servicedesk_agent.infra.tenancy import TenantRegistry
from servicedesk_agent.infra.tenant_context import tenant_scope
from tests.support.fake_model import scripted_model
from tests.test_workflow import ALICE
from servicedesk_agent.agent.tooling.tool_spec import make_tool
from servicedesk_agent.workflow.store import WorkflowStore


async def okay(messages, info):
    return ModelResponse(parts=[TextPart("查询完成")])


def configured(tmp_path, *, config=None, models=None, engine=None):
    config = copy.deepcopy(yaml.safe_load(DEFAULT_CONFIG.read_text())) if config is None else config
    config["models"].pop("gpt-6.2-sol", None)
    config["models"]["backup"] = copy.deepcopy(config["models"]["deepseek-flash"])
    for route in config["routes"].values():
        route["thinking"] = "disabled"
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


def test_failure_switches_model_and_recovery_restores_preference(tmp_path):
    router = configured(tmp_path)
    first = router.pick("agent_read", "default")
    for _ in range(5):
        router.record(first, success=False)
    assert router.pick("agent_read", "default").name == "backup"
    router.breakers[first.name].opened_at -= 61
    probe = router.pick("agent_read", "default")
    router.record(probe, success=True)
    selected = router.pick("agent_read", "default")
    assert selected.name == first.name and selected.reason == "primary"


def test_hot_reload_keeps_open_circuit_of_unchanged_model_and_resets_edited_one(tmp_path):
    router = configured(tmp_path)
    first = router.pick("agent_read", "default")
    for _ in range(5):
        router.record(first, success=False)
    data = router.config.model_dump()
    data["routes"]["agent_read"]["max_rounds"] = 4      # unrelated edit
    router.path.write_text(yaml.safe_dump(data))
    assert router.pick("agent_read", "default").name == "backup"
    data["models"][first.name]["data_classes"].append("confidential")   # the model's own settings change
    router.path.write_text(yaml.safe_dump(data))
    assert router.pick("agent_read", "default").name == first.name


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


def test_budget_preserves_tools_and_blocks_write_execution(tmp_path, db_url):
    store = WorkflowStore(db_url)
    TenantRegistry(store.engine).create("budget", actor="system", quotas={"daily_cost_usd": 0})
    seen = []
    async def respond(messages, info):
        seen.append({tool.name for tool in info.function_tools})
        if len(seen) == 1:
            from pydantic_ai.messages import ToolCallPart
            return ModelResponse(parts=[ToolCallPart("create_ticket", {})])
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
    assert result.success and all(tools == {"diagnose", "create_ticket"} for tools in seen)
    assert not result.tools_used and result.tool_traces[0]["success"] is False
    assert result.observability["route_reason"] == "budget"
    assert router.pick("agent_read", "default").reason != "budget"


def test_invalid_hot_reload_keeps_last_good_config_and_valid_reload_applies(tmp_path, db_url):
    from servicedesk_agent.infra.db import change_events
    from sqlalchemy import select
    store = WorkflowStore(db_url)
    router = configured(tmp_path, engine=store.engine)
    kept = router.fingerprint
    data = router.config.model_dump()
    data["routes"]["agent_read"]["max_rounds"] = 0
    router.path.write_text(yaml.safe_dump(data))
    assert router.pick("agent_read", "default").max_rounds == 15
    assert router.pick("agent_read", "default").max_rounds == 15
    assert router.fingerprint == kept
    router.path.write_text("routes: [")
    assert router.pick("agent_read", "default").max_rounds == 15
    data["routes"]["agent_read"]["max_rounds"] = 18
    router.path.write_text(yaml.safe_dump(data))
    assert router.pick("agent_read", "default").max_rounds == 18
    with store.engine.connect() as db:
        kinds = db.execute(select(change_events.c.kind, change_events.c.detail).order_by(change_events.c.id)).all()
    assert [kind for kind, _ in kinds] == ["model_route_rejected", "model_route_rejected", "model_route"]
    assert kinds[0].detail["kept"] == kept


def test_thinking_is_route_config_not_an_environment_toggle(tmp_path):
    router = configured(tmp_path)
    route = router.pick("agent_write", "default")
    assert route.settings["extra_body"] == {"thinking": {"type": "disabled"}}
    assert route.max_rounds == 20
    data = router.config.model_dump()
    data["routes"]["agent_write"]["thinking"] = "max"
    router.path.write_text(yaml.safe_dump(data))
    settings = router.pick("agent_write", "default").settings
    assert settings["max_tokens"] == 16000 and settings["extra_body"]["reasoning_effort"] == "max"
    assert "temperature" not in settings


def test_models_without_a_thinking_api_get_no_vendor_parameters(tmp_path):
    config = yaml.safe_load(DEFAULT_CONFIG.read_text())
    config["models"]["deepseek-flash"]["thinking_api"] = "none"
    for route_name in ("agent_read", "agent_write"):
        config["routes"][route_name]["thinking"] = "disabled"
    router = configured(tmp_path, config=config)
    settings = router.pick("agent_read", "default").settings
    assert "extra_body" not in settings and settings["temperature"] == .2
    config["routes"]["agent_write"]["thinking"] = "low"
    with pytest.raises(ValidationError, match="thinking API"):
        RoutingConfig.model_validate(config)


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
    router = ModelRouter(models=dict.fromkeys(["deepseek-flash", "gpt-6.2-sol"], FunctionModel(respond)))
    agent = BaseAgent(None, "", router=router)
    agent.set_shared_tools({"diagnose": make_tool("diagnose", "诊断", {}, lambda req, args: {"ok": True})})
    result = asyncio.run(agent.handle(Request("检查VPN", allowed_writes=frozenset())))
    assert not result.success and result.error_code == "model_unavailable"
    assert result.token_usage["input_tokens"] == 1000 and result.token_usage["output_tokens"] == 20
    assert result.observability["cost_usd"] == pytest.approx(router.cost("deepseek-flash", result.token_usage))


def test_prompt_cache_key_is_scoped_and_opt_in(tmp_path):
    data = yaml.safe_load(DEFAULT_CONFIG.read_text())
    router = configured(tmp_path, config=data)
    assert "prompt_cache_key" not in router.pick("agent_read", conv_id="c").settings["extra_body"]
    data["models"]["deepseek-flash"]["prompt_cache_mode"] = "openai_key"
    router = configured(tmp_path, config=data)

    def key(tenant, conv):
        body = router.pick("agent_read", tenant, conv_id=conv).settings["extra_body"]
        assert "thinking" in body
        return body["prompt_cache_key"]

    digest = key("tenant-a", "employee@example.org")
    assert len(bytes.fromhex(digest)) == 32 and digest == key("tenant-a", "employee@example.org")
    assert digest != key("tenant-b", "employee@example.org") and digest == key("tenant-a", "other")
    assert digest == key("tenant-a", None)
    data["models"]["deepseek-flash"]["prompt_cache_scope"] = "conversation"
    router = configured(tmp_path, config=data)
    assert key("tenant-a", "first") != key("tenant-a", "second")


@pytest.mark.parametrize("endpoint", [None, "https://gpt.test/v1"])
def test_responses_provider_and_route_settings_are_isolated(monkeypatch, endpoint):
    from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
    from servicedesk_agent.agent.masking.model_masking import MaskingTransport
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-ds")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://ds.test/v1")
    monkeypatch.setenv("OPENAI_MODEL", "deepseek-flash")
    monkeypatch.setenv("OPENAI_GPT_API_KEY", "synthetic-gpt")
    monkeypatch.delenv("OPENAI_GPT_BASE_URL", raising=False)
    if endpoint:
        monkeypatch.setenv("OPENAI_GPT_BASE_URL", endpoint)
    router = ModelRouter()
    for route in ("agent_read", "agent_write"):
        default_choice = router.pick(route, conv_id="one")
        assert default_choice.name == default_choice.model.model_name == "deepseek-flash"
        assert default_choice.settings["extra_body"] == {"thinking": {"type": "enabled"}, "reasoning_effort": "low"}
        choice = router.pick(route, model="gpt-6.2-sol", thinking="high", conv_id="one")
        assert choice.name == choice.model.model_name == "gpt-6.2-sol"
        assert isinstance(choice.model, OpenAIResponsesModel)
        assert choice.settings["openai_prompt_cache_retention"] == "24h"
        assert choice.settings["openai_reasoning_effort"] == "high"
        assert choice.settings["openai_store"] is False and choice.settings["max_tokens"] == 16000
        assert "temperature" not in choice.settings and len(choice.settings["openai_prompt_cache_key"]) == 64
        assert str(choice.model.client.base_url) == (endpoint or "https://api.openai.com/v1") + "/"
        assert choice.model.client.api_key == "synthetic-gpt"
    ds = router.pick("summary").model
    assert isinstance(ds, OpenAIChatModel) and str(ds.client.base_url) == "https://ds.test/v1/"
    assert ds.client.api_key == "synthetic-ds"
    for model in router.models.values():
        assert isinstance(model.client._client._transport, MaskingTransport)


def test_requested_primary_can_fail_over_without_changing_default(tmp_path):
    router = configured(tmp_path)
    preferred = router.pick("agent_read", model="backup")
    assert preferred.name == "backup"
    for _ in range(5):
        router.record(preferred, success=False)
    assert router.pick("agent_read", model="backup").name == "deepseek-flash"
    assert router.pick("agent_read").reason == "primary"
