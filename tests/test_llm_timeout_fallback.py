"""Offline classification tests for transient LLM service failures; no network or model calls."""
import asyncio

import pytest
from pydantic_ai.models.function import FunctionModel

from agents.base_agent import BaseAgent
from agents.types import ModelUnavailableError, Request


def _raising_model(exc: Exception):
    async def respond(messages, info):
        raise exc

    return FunctionModel(respond)


def _agent(exc: Exception):
    return BaseAgent(_raising_model(exc), model="test")


def test_timeout_error_is_classified_as_model_unavailable():
    async def run():
        agent = _agent(TimeoutError("upstream turn timed out"))
        with pytest.raises(ModelUnavailableError):
            await agent._call_llm(Request(message="帮我重置密码", request_id="t1"))

    asyncio.run(run())


def test_non_transient_error_is_not_wrapped_as_model_unavailable():
    async def run():
        agent = _agent(ValueError("工具参数 schema 非法"))
        with pytest.raises(ValueError):
            await agent._call_llm(Request(message="帮我重置密码", request_id="t2"))

    asyncio.run(run())
