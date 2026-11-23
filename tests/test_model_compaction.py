import asyncio

import pytest
from fakeredis.aioredis import FakeRedis
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from servicedesk_agent.agent.base_agent import BaseAgent
from servicedesk_agent.agent.memory.context_budget import message_text, text_tokens
from servicedesk_agent.agent.memory.conversation_memory import MemoryManager
from servicedesk_agent.agent.tooling.executor import WriteGuardBlocked
from servicedesk_agent.agent.types import Request
from tests.test_pydantic_ai_runtime import _agent


@pytest.mark.parametrize(("name", "slot", "blocked"), [
    ("request_access", "ticket_id", False), ("lock_device", "asset_id", True),
    ("unlock_account", "employee_id", True), ("request_access", "unknown", True),
])
def test_action_slot_ownership(name, slot, blocked):
    req = Request("查询工单并申请权限", conversation_state={"missing_fields": [slot]})
    args = dict(employee_id="E1", system="GitLab", role="reporter", reason="审核", duration_hours=24)
    call = BaseAgent._guard_unsafe_tool(req, name, args)
    if blocked:
        with pytest.raises(WriteGuardBlocked):
            asyncio.run(call)
    else:
        asyncio.run(call)
        assert req.tool_attempts[0]["tool_name"] == name


def test_reactive_compaction_retries_only_the_failed_tool_result_request():
    async def run():
        calls = []
        async def respond(messages, info):
            calls.append(messages)
            if len(calls) == 1:
                return ModelResponse(parts=[ToolCallPart("diagnose", {"code": "vpn-691"}, tool_call_id="c1")])
            if len(calls) == 2:
                raise ModelHTTPError(400, "gpt", {"code": "context_length_exceeded"})
            assert any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts)
            assert messages[0].parts[0].content == "交接"
            return ModelResponse(parts=[TextPart("完成")])

        async def compact(selection, keep):
            return [ModelRequest.user_text_prompt("交接")]
        agent = _agent(respond)
        req = Request("VPN故障", model_history=[ModelRequest.user_text_prompt("以前"), ModelResponse(parts=[TextPart("已知")])],
                      compact_history=compact)
        result = await agent.handle(req)
        assert result.success and result.tools_used == ["diagnose"] and len(calls) == 3
    asyncio.run(run())


def test_model_switch_and_bounded_summary(tmp_path):
    async def run():
        sizes = []
        async def respond(messages, info):
            sizes.append(sum(text_tokens(message_text(m)) for m in messages))
            return ModelResponse(parts=[TextPart("VPN-691 仍待处理")])
        agent = _agent(respond)
        assert agent.router.compaction_budget("gpt-6.2-sol") == (240000, 24000)
        assert agent.router.compaction_budget("deepseek-flash") == (968000, 24000)
        memory = MemoryManager(database_url=f"sqlite:///{tmp_path}/memory.db", router=agent.router)
        memory._redis = FakeRedis(decode_responses=True)
        memory.context_mode = "working"
        # A smaller configured summary model forces multiple passes on the same 300k-token history.
        summary_sizes = []
        async def summarize(messages, info):
            summary_sizes.append(sum(text_tokens(message_text(m)) for m in messages))
            return ModelResponse(parts=[TextPart("摘要 VPN-691")])
        async def compact(selection, keep):
            return await memory.compact_for_model("u", "c", selection, keep, lambda: agent.summary_prefix(req))
        for i in range(10):
            ctx = await memory.get_context("u", "c")
            for model in ("deepseek-flash", "gpt-6.2-sol"):
                req = Request("网络故障。" * 300, model=model, model_history=ctx.model_history(), compact_history=compact)
                assert (await agent.handle(req)).success
            await memory.commit_turn("u", "c", str(i), req.message, {"response": "VPN-691"})
        ctx = await memory.get_context("u", "c")
        assert not ctx.summary and len(ctx.model_history()) == 20
        assert len(sizes) == 20 and 15000 <= max(sizes) < 240000
        for i in range(10):
            await memory.commit_turn("u", "c", str(i+10), "网络故障。" * 6000, {"response": "待处理"})
        ctx = await memory.get_context("u", "c")
        state = {}
        req = Request("继续", model="deepseek-flash", model_history=ctx.model_history(), compact_history=compact, conversation_state=state)
        assert (await agent.handle(req)).success and sizes[-1] > 300000
        assert state["last_context_model"] == "deepseek-flash"
        agent.router.models["deepseek-flash"] = FunctionModel(summarize)
        agent.router.config.models["deepseek-flash"] = agent.router.config.models["deepseek-flash"].model_copy(update={"context_window": 132000})
        req = Request("继续", model="gpt-6.2-sol", model_history=ctx.model_history(), compact_history=compact, conversation_state=state)
        assert (await agent.handle(req)).success and sizes[-1] < 240000
        assert len(summary_sizes) > 1 and max(summary_sizes) < 100000
        assert state["last_context_model"] == "gpt-6.2-sol"
        await memory.close()
    asyncio.run(run())


def test_redis_quota_archives_complete_turns_and_refreshes_ttls(tmp_path):
    async def run():
        memory = MemoryManager(database_url=f"sqlite:///{tmp_path}/memory.db")
        memory._redis = FakeRedis(decode_responses=True)
        for i in range(50):
            await memory.commit_turn("u", "c", str(i), "问题", {"response": "答复"})
        assert len(await memory._get_working_memory("u", "c")) == 100
        memory.WORKING_MAX_BYTES = 1200
        await memory.commit_turn("u", "c", "50", "继续", {"response": "答复"}, state={"last_context_tokens": 50})
        key = memory._wm_key("u", "c")
        assert sum(len(v.encode()) for v in await memory._redis.lrange(key, 0, -1)) <= 1200
        recent = await memory._get_working_memory("u", "c")
        assert recent and [m.role.value for m in recent] == ["user", "assistant"] * (len(recent)//2)
        assert await memory._redis.hlen(memory._archive_key("u", "c")) == 1
        assert await memory._redis.ttl(key) == await memory._redis.ttl(memory._summary_key("u", "c"))
        assert "last_context_tokens" not in await memory.get_state("u", "c")
        await memory.close()
    asyncio.run(run())


def test_measured_context_only_reused_for_matching_prefix():
    from pydantic_ai.usage import RequestUsage
    async def run():
        compacted = []
        async def respond(messages, info):
            return ModelResponse(parts=[TextPart("完成")], usage=RequestUsage(input_tokens=239999, output_tokens=1))
        async def compact(selection, keep):
            compacted.append(selection.name)
            return [ModelResponse(parts=[TextPart("交接")])]
        agent = _agent(respond)
        state = {}
        first = await agent.handle(Request("VPN", model="gpt-6.2-sol", conversation_state=state))
        original = dict(state)
        assert state["last_context_tokens"] == 240000
        for history in ([ModelRequest.user_text_prompt("修改后的前缀"), first.new_messages[-1]], first.new_messages):
            result = await agent.handle(Request("继续", model="gpt-6.2-sol", model_history=history, conversation_state=dict(original),
                                                compact_history=compact))
            assert result.success
            assert len(compacted) == int(history is first.new_messages)
    asyncio.run(run())


def test_single_oversize_input_returns_explicit_error_without_provider_call():
    async def run():
        async def unexpected(messages, info):
            pytest.fail("oversize input reached provider")
        agent = _agent(unexpected)
        req = Request("文" * 272001, model="gpt-6.2-sol")
        result = await agent.handle(req)
        assert result.error_code == "context_length_exceeded" and "缩短" in result.content
    asyncio.run(run())
