"""core/jev.py：请求格式、解析、重试只针对 429/5xx、超时上限、环境变量配置。HTTP 用 MockTransport，不联网。"""
import asyncio
import json

import httpx2
import pytest

from core.jev import JevClient, top_choice

ANSWERS = {"intent": {"type": "choice", "choice": "vpn_issue", "confidence": .8, "probabilities": {"vpn_issue": .9, "wifi_issue": .1}}}


def client_with(handler, **kwargs) -> JevClient:
    client = JevClient("secret-key", **kwargs)
    client._client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler), headers={"Authorization": "Bearer secret-key"})
    return client


def ok(answers=ANSWERS):
    return httpx2.Response(200, json={"model": "jev-1.14.0", "answers": answers, "usage": {"input_tokens": 400, "output_tokens": 3}})


def test_ask_sends_state_questions_model_and_bearer_and_parses_response():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["Authorization"]
        seen["body"] = json.loads(request.content)
        return ok()

    async def run():
        client = client_with(handler, model="jev-latest")
        response = await client.ask("用户最新消息：VPN 连不上", {"intent": {"type": "choice", "criteria": {"vpn_issue": "VPN"}}})
        await client.aclose()
        return response

    response = asyncio.run(run())
    assert seen["auth"] == "Bearer secret-key"
    assert seen["body"] == {"model": "jev-latest", "state": "用户最新消息：VPN 连不上", "questions": {"intent": {"type": "choice", "criteria": {"vpn_issue": "VPN"}}}}
    assert response.answers == ANSWERS and response.model == "jev-1.14.0" and response.usage == {"input_tokens": 400, "output_tokens": 3}


@pytest.mark.parametrize("status", [429, 500, 503])
def test_retryable_status_is_retried_then_succeeds(monkeypatch, status):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx2.Response(status) if len(calls) == 1 else ok()

    async def no_sleep(_):
        return None

    monkeypatch.setattr("core.jev.asyncio.sleep", no_sleep)

    async def run():
        client = client_with(handler, retries=1)
        try:
            return await client.ask("s", {})
        finally:
            await client.aclose()

    assert asyncio.run(run()).answers == ANSWERS and len(calls) == 2


def test_client_errors_are_not_retried_and_raise():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx2.Response(401, json={"error": "bad key"})

    async def run():
        client = client_with(handler, retries=3)
        try:
            await client.ask("s", {})
        finally:
            await client.aclose()

    with pytest.raises(httpx2.HTTPStatusError):
        asyncio.run(run())
    assert len(calls) == 1


def test_online_default_has_no_retries_so_failures_surface_immediately():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx2.Response(503)

    async def run():
        client = client_with(handler)
        try:
            await client.ask("s", {})
        finally:
            await client.aclose()

    with pytest.raises(httpx2.HTTPStatusError):
        asyncio.run(run())
    assert len(calls) == 1


def test_total_timeout_bounds_a_slow_response():
    async def handler(request):
        await asyncio.sleep(1)
        return ok()

    async def run():
        client = client_with(handler, timeout_s=.05)
        try:
            await client.ask("s", {})
        finally:
            await client.aclose()

    with pytest.raises(TimeoutError):
        asyncio.run(run())


def test_from_env_requires_key_and_reads_model(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert JevClient.from_env() is None
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    monkeypatch.setenv("TYPESAFE_MODEL", "jev-1.14.0")
    client = JevClient.from_env(timeout_s=1.5)
    assert client.model == "jev-1.14.0" and client._timeout_s == 1.5 and client._retries == 0
    asyncio.run(client.aclose())


def test_top_choice_uses_probability_of_choice_and_falls_back_to_confidence():
    assert top_choice(ANSWERS["intent"]) == ("vpn_issue", .9)
    assert top_choice({"choice": "none", "confidence": .4}) == ("none", .4)
    assert top_choice({"choice": "a", "probabilities": {}}) == ("a", 0.)
