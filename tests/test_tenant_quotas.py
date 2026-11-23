"""Tenant quotas and fair queues: one tenant cannot crowd out the others (ADR 0035)."""
import asyncio
from dataclasses import replace

import pytest

from it_copilot.api import deps
from tests.runtime import state
from it_copilot.infra import quotas
from it_copilot.infra.db import shared_engine
from it_copilot.infra.fair_queue import rotate
from it_copilot.infra.identity import Principal
from it_copilot.infra.rate_limit import MemoryRateLimiter, RateRule
from it_copilot.infra.tenancy import TenantRegistry

from tests.test_workflow import ALICE, BOB, access, env  # noqa: F401


# ── Quotas ──────────────────────────────────────────────────────────────────

def test_quotas_are_validated_when_written():
    for bad in ({"agent_turn": "600/m"}, {"daily_cost_usd": -1}, {"daily_cost_usd": float("nan")}, {"burst": 3}, ["agent_turn"]):
        with pytest.raises(quotas.QuotaError):
            quotas.validate(bad)
    assert quotas.validate({"agent_turn": " OFF "}) == {"agent_turn": "off"}
    assert quotas.turn_rule({"quotas": {}}) == RateRule.parse(quotas.DEFAULT_AGENT_TURN, None)
    assert quotas.turn_rule({"quotas": {"agent_turn": "off"}}) is None
    assert quotas.turn_rule({"quotas": {"agent_turn": "2/60"}}) == RateRule(2, 60)


def test_quota_changes_are_audited(db_url):
    from sqlalchemy import select

    from it_copilot.infra.db import admin_audit

    registry = TenantRegistry(shared_engine(db_url), cache_ttl_s=0)
    registry.create("acme", actor="cli:op", quotas={"agent_turn": "10/60"})
    with pytest.raises(quotas.QuotaError):
        registry.set_quotas("acme", {"agent_turn": "fast"}, actor="cli:op")
    registry.set_quotas("acme", {"daily_cost_usd": 5}, actor="cli:op")
    registry.set_quotas("acme", {"daily_cost_usd": 5}, actor="cli:op")    # unchanged: no entry
    assert registry.get("acme")["quotas"] == {"daily_cost_usd": 5}
    with registry.engine.connect() as db:
        details = db.execute(select(admin_audit.c.detail).where(admin_audit.c.event == "tenant_quotas_changed")).scalars().all()
    assert details == [{"from": {"agent_turn": "10/60"}, "to": {"daily_cost_usd": 5}}]


# ── Two-layer rate limit ────────────────────────────────────────────────────

@pytest.fixture
def tenants(db_url, monkeypatch):
    registry = TenantRegistry(shared_engine(db_url), cache_ttl_s=0)
    registry.create("acme", actor="cli:op", quotas={"agent_turn": "2/60"})
    registry.create("globex", actor="cli:op", quotas={"agent_turn": "2/60"})
    monkeypatch.setattr(state, "tenants", registry)
    return registry


def member(name, tenant):
    return Principal(f"dev:{name}", name, "engineering", frozenset({"employee"}), tenant=tenant)


def test_a_tenant_shares_one_budget_and_other_tenants_keep_theirs(tenants, monkeypatch):
    monkeypatch.setattr(state, "rate_limiter", MemoryRateLimiter({"agent_turn": RateRule(30, 60)}))

    async def turns():
        decide = lambda p: deps.rate_limit_decision(state, p, "agent_turn", "http")  # noqa: E731
        acme = [(await decide(member(n, "acme"))).allowed for n in ("alice", "bob", "carol")]
        globex = [(await decide(member(n, "globex"))).allowed for n in ("alice", "bob")]
        return acme, globex
    acme, globex = asyncio.run(turns())
    assert acme == [True, True, False]     # carol is under her own limit, but acme has spent its two
    assert globex == [True, True]


def test_a_user_refused_by_their_own_limit_does_not_spend_the_tenants_budget(tenants, monkeypatch):
    monkeypatch.setattr(state, "rate_limiter", MemoryRateLimiter({"agent_turn": RateRule(1, 60)}))

    async def turns():
        decide = lambda p: deps.rate_limit_decision(state, p, "agent_turn", "http")  # noqa: E731
        alice = [(await decide(member("alice", "acme"))).allowed for _ in range(5)]
        return alice, (await decide(member("bob", "acme"))).allowed
    alice, bob = asyncio.run(turns())
    assert alice == [True, False, False, False, False] and bob


# ── Fair execution queue ────────────────────────────────────────────────────

def approve_access(store, tenant, n):
    """``n`` approved requests on distinct resources, so nothing serializes them but the queue order."""
    for i in range(n):
        store.clock.t += 1  # Distinct due times define FIFO order on both database engines.
        requester = Principal(f"dev:u{i}", f"u{i}", "engineering", frozenset({"employee"}), str(3000 + i), tenant=tenant)
        pending = store.create(f"{tenant}-{i}", request_id=f"r-{tenant}-{i}", conv_id="c", principal=requester,
                               action_requests=[access(employee=str(3000 + i))])
        store.decide(f"{tenant}-{i}", "approved", replace(BOB, tenant=tenant), pending["payload_hash"])


def claim_order(store, n, **kwargs):
    order, held, last = [], set(), None
    for _ in range(n):
        claimed = store.claim("w", 60, 1, exclude_ids=held, after_tenant=last, **kwargs)
        if not claimed:
            break
        held.add(claimed[0]["id"])
        last = claimed[0]["tenant"]
        order.append(last)
    return order


def test_a_tenant_with_a_backlog_does_not_make_the_next_tenant_wait_behind_all_of_it(env):  # noqa: F811
    store, _, clock = env
    approve_access(store, "noisy", 6)
    clock.t += 10                                                   # quiet's request is the newest
    approve_access(store, "quiet", 2)
    assert claim_order(store, 8) == ["noisy", "quiet", "noisy", "quiet", "noisy", "noisy", "noisy", "noisy"]


def test_one_tenants_queue_keeps_its_order(env):  # noqa: F811
    store, _, _ = env
    approve_access(store, "solo", 4)
    held, got = set(), []
    for _ in range(5):
        claimed = store.claim("w", 60, 1, exclude_ids=held)
        held |= {row["id"] for row in claimed}
        got += [row["approval_id"] for row in claimed]
    assert got == ["solo-0", "solo-1", "solo-2", "solo-3"]


def test_rotation_serves_every_tenants_oldest_before_anyones_second():
    rows = [{"tenant": t, "tenant_rank": r} for t, r in [("a", 1), ("a", 2), ("b", 1), ("c", 1), ("c", 2)]]
    assert [(r["tenant"], r["tenant_rank"]) for r in rotate(rows, "b")] == [("c", 1), ("a", 1), ("b", 1), ("c", 2), ("a", 2)]


# ── Fair shared-task queue ──────────────────────────────────────────────────
