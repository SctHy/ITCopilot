"""ToolExecutor governance and Gitea adapter failure classification (no network)."""
import asyncio
import time

import httpx
import pytest

from adapters.base import AmbiguousError, PermanentError, TransientError
from adapters.gitea import GiteaAdapter
from agents.tools import make_tool
from tooling.executor import ToolExecutor, ToolTimeout, ToolUnavailable


def spec(handler, retry_safe=True, name="probe"):
    return make_tool(name, "probe", {}, handler, retry_safe=retry_safe)


def test_read_only_tools_retry_but_side_effecting_tools_do_not():
    calls = {"read": 0, "write": 0}

    def flaky(kind):
        def handler(req, args):
            calls[kind] += 1
            if calls[kind] == 1:
                raise ConnectionError("blip")
            return {"ok": True}
        return handler

    executor = ToolExecutor(backoff_s=0)
    assert asyncio.run(executor.run(spec(flaky("read")), None, {})) == {"ok": True}
    with pytest.raises(ConnectionError):
        asyncio.run(executor.run(spec(flaky("write"), retry_safe=False, name="write"), None, {}))
    assert calls == {"read": 2, "write": 1}


def test_timeout_and_circuit_breaker():
    async def slow(req, args):
        await asyncio.sleep(1)

    executor = ToolExecutor(timeout_s=0.02, read_retries=0, failure_threshold=2, recovery_s=60)
    for _ in range(2):
        with pytest.raises(ToolTimeout):
            asyncio.run(executor.run(spec(slow), None, {}))
    started = time.monotonic()
    with pytest.raises(ToolUnavailable):
        asyncio.run(executor.run(spec(slow), None, {}))
    assert time.monotonic() - started < 0.02  # open breaker fails fast
    assert executor.snapshot()["probe"]["state"] == "open"


def test_validation_errors_do_not_trip_the_breaker():
    def invalid(req, args):
        raise ValueError("bad input")

    executor = ToolExecutor(failure_threshold=1)
    for _ in range(3):
        with pytest.raises(ValueError):
            asyncio.run(executor.run(spec(invalid), None, {}))
    assert executor.snapshot()["probe"]["state"] == "closed"


def gitea(handler):
    return GiteaAdapter("http://gitea", "t", {"gitlab": "it/platform"}, lambda e: {"1001": "alice"}.get(e),
                        transport=httpx.MockTransport(handler))


ACTION = {"tool_name": "request_access", "arguments": {"employee_id": "1001", "system": "gitlab", "role": "developer"}}


def test_gitea_grant_observe_and_revoke_against_a_fake_server():
    collaborators = {}

    def handler(request: httpx.Request):
        path = request.url.path
        if path == "/api/v1/repos/it/platform/collaborators/alice":
            if request.method == "PUT":
                collaborators["alice"] = __import__("json").loads(request.content)["permission"]
                return httpx.Response(204)
            if request.method == "DELETE":
                collaborators.pop("alice", None)
                return httpx.Response(204)
            return httpx.Response(204 if "alice" in collaborators else 404)
        if path == "/api/v1/repos/it/platform/collaborators/alice/permission":
            return httpx.Response(200, json={"permission": collaborators.get("alice", "read")})
        return httpx.Response(200, json={})

    adapter = gitea(handler)

    async def run():
        assert await adapter.current_level("1001", "gitlab") is None
        await adapter.apply("a1", ACTION)
        assert collaborators == {"alice": "write"} and await adapter.observe("a1", ACTION)
        revoke = {"tool_name": "revoke_access", "arguments": {**ACTION["arguments"], "baseline_level": None}}
        await adapter.apply("a2", revoke)
        assert collaborators == {} and await adapter.observe("a2", revoke)
        await adapter.close()

    asyncio.run(run())


@pytest.mark.parametrize("failure,expected", [
    (httpx.ConnectError("refused"), TransientError),
    (httpx.ReadTimeout("slow"), AmbiguousError),
    (httpx.Response(502), AmbiguousError),
    (httpx.Response(403), PermanentError),
])
def test_gitea_failure_classification(failure, expected):
    def handler(request):
        if isinstance(failure, Exception):
            raise failure
        return failure

    with pytest.raises(expected):
        asyncio.run(gitea(handler).apply("a1", ACTION))


def test_gitea_unknown_user_is_permanent():
    adapter = GiteaAdapter("http://gitea", "t", {"gitlab": "it/platform"}, lambda e: None,
                           transport=httpx.MockTransport(lambda r: httpx.Response(204)))
    with pytest.raises(PermanentError, match="Gitea 账号"):
        asyncio.run(adapter.preflight(ACTION))
