"""Per-identity rate limit on entries that start model work: GCRA semantics, shared Redis budget, HTTP/IM behaviour."""
import asyncio

import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient

import api.main as api
from api import state
from api.routes.chat import RATE_LIMITED_REPLY, _channel_turn
from core.identity import Principal
from core.rate_limit import AGENT_TURN, DEFAULT_RULES, MemoryRateLimiter, RateRule, RedisRateLimiter, rules_from_env
from tests.conftest import auth

ALICE = Principal("dev:alice", "alice", "engineering", frozenset({"employee"}), "1001")


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_rule_parsing_disables_or_keeps_the_default_on_bad_input():
    default = DEFAULT_RULES[AGENT_TURN]
    assert RateRule.parse("10/30", default) == RateRule(10, 30.0)
    assert RateRule.parse("", default) == default
    for off in ("0", "off", "false"):
        assert RateRule.parse(off, default) is None
    for bad in ("ten/30", "10", "0/60", "10/0", "10/nan", "-1/60"):
        assert RateRule.parse(bad, default) == default
    assert rules_from_env(lambda name: "off") == {}
    assert rules_from_env(lambda name: None) == DEFAULT_RULES


def test_burst_then_steady_rate_and_denials_do_not_consume_capacity():
    clock = Clock()
    limiter = MemoryRateLimiter({AGENT_TURN: RateRule(3, 60)}, clock=clock)
    check = lambda who="a": asyncio.run(limiter.check(AGENT_TURN, who)).allowed
    assert [check() for _ in range(3)] == [True, True, True]      # idle caller gets the whole burst
    denied = asyncio.run(limiter.check(AGENT_TURN, "a"))
    assert not denied.allowed and denied.retry_after_s == pytest.approx(20)
    assert check("b")                                              # budgets are per identity
    for _ in range(50):                                            # hammering while denied ...
        check()
    clock.t += 20
    assert check() and not check()                                 # ... does not push the next slot further out
    assert asyncio.run(limiter.check("unknown_bucket", "a")).allowed


def test_redis_backend_shares_one_budget_across_processes():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    rules = {AGENT_TURN: RateRule(2, 60)}
    first, second = RedisRateLimiter(redis, rules), RedisRateLimiter(redis, rules)   # two API processes

    async def run():
        results = [(await first.check(AGENT_TURN, "a")).allowed, (await second.check(AGENT_TURN, "a")).allowed]
        denied = await first.check(AGENT_TURN, "a")
        other = await second.check(AGENT_TURN, "b")
        ttl = await redis.pttl("ratelimit:agent_turn:a")
        return results, denied, other, ttl

    results, denied, other, ttl = asyncio.run(run())
    assert results == [True, True] and not denied.allowed and 0 < denied.retry_after_s <= 30
    assert other.allowed and 0 < ttl <= 60_001                      # keys expire once the budget is full again


def test_redis_errors_fail_open():
    class Broken:
        def register_script(self, _):
            async def script(**_):
                raise ConnectionError("redis down")
            return script

    assert asyncio.run(RedisRateLimiter(Broken(), DEFAULT_RULES).check(AGENT_TURN, "a")).allowed


@pytest.fixture
def limited(monkeypatch):
    limiter = MemoryRateLimiter({AGENT_TURN: RateRule(1, 60)})
    monkeypatch.setattr(state, "rate_limiter", limiter)
    monkeypatch.setattr(state, "orchestrator", None)  # an allowed request then stops at "service not ready"
    return limiter


def test_chat_returns_429_with_retry_after_per_identity(limited):
    http = TestClient(api.app)
    body = {"message": "VPN 连不上", "conv_id": "c1"}
    assert http.post("/chat", json=body, headers=auth("alice")).status_code == 503
    denied = http.post("/chat", json=body, headers=auth("alice"))
    assert denied.status_code == 429 and 1 <= int(denied.headers["Retry-After"]) <= 60
    assert http.post("/chat", json=body, headers=auth("bob")).status_code == 503    # bob has his own budget
    assert http.post("/chat", json=body).status_code == 401                         # identity is checked first


def test_im_messages_get_one_short_reply_instead_of_model_work(limited):
    asyncio.run(limited.check(AGENT_TURN, ALICE.scope_key))  # alice already used her budget on the web
    reply = asyncio.run(_channel_turn(ALICE, "VPN 连不上", "feishu:oc_1", "om_1"))
    assert reply["response"] == RATE_LIMITED_REPLY and reply["approval_required"] is False


def test_rate_limiter_is_off_without_configuration(monkeypatch):
    monkeypatch.setattr(state, "rate_limiter", None)
    monkeypatch.setattr(state, "orchestrator", None)
    http = TestClient(api.app)
    for _ in range(5):
        assert http.post("/chat", json={"message": "hi", "conv_id": "c"}, headers=auth("alice")).status_code == 503


def test_shared_task_messages_count_only_when_they_ask_the_agent(limited, monkeypatch):
    monkeypatch.setattr(state, "tasks", None)  # an allowed request then stops at "not ready"
    http = TestClient(api.app)
    post = lambda ask: http.post("/tasks/t1/messages", json={"message": "进展如何", "client_request_id": "m", "ask_agent": ask},
                                 headers=auth("alice")).status_code
    assert post(True) == 503 and post(True) == 429
    assert post(False) == 503   # plain discussion is not limited
