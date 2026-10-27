"""Offline classification tests for transient gateway/LLM failures; no network or model calls."""
import asyncio

import pytest
from pydantic_ai.models.function import FunctionModel

from agents.agent_orchestrator import (
    AccessAgent,
    AgentType,
    GatewayUnavailableError,
    Request,
    profile,
)


def _raising_model(exc: Exception):
    async def respond(messages, info):
        raise exc

    return FunctionModel(respond)


def _agent(exc: Exception):
    agent = AccessAgent(_raising_model(exc), model="test", profile=profile("access", "处理账号", []))
    agent.agent_type = AgentType.ACCESS
    return agent


def test_timeout_error_is_classified_as_gateway_unavailable():
    async def run():
        agent = _agent(TimeoutError("upstream turn timed out"))
        with pytest.raises(GatewayUnavailableError):
            await agent._call_llm(Request(message="帮我重置密码", request_id="t1"))

    asyncio.run(run())


def test_non_transient_error_is_not_wrapped_as_gateway_unavailable():
    async def run():
        agent = _agent(ValueError("工具参数 schema 非法"))
        with pytest.raises(ValueError):
            await agent._call_llm(Request(message="帮我重置密码", request_id="t2"))

    asyncio.run(run())
