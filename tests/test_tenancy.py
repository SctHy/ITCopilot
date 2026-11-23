"""Current tenant registry, trusted bindings and tenant isolation."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests import runtime as api
from tests.runtime import state
from servicedesk_agent.infra.db import admin_audit, shared_engine
from servicedesk_agent.infra.tenancy import TenantExists, TenantRegistry, TenantStatus, TenantUnavailable
from tests.conftest import auth


@pytest.fixture
def registry(db_url):
    return TenantRegistry(shared_engine(db_url), cache_ttl_s=0)


def test_create_validates_ids_refuses_duplicates_and_audits_under_the_tenant(registry):
    for bad in ("", "Acme", "_platform", "*", "a b", "x" * 81):
        with pytest.raises(ValueError):
            registry.create(bad, actor="cli:op")
    registry.create("acme", actor="cli:op", tier="enterprise", issuer="https://sso.acme/realms/acme")
    for duplicate in ({"tenant": "acme"}, {"tenant": "other", "issuer": "https://sso.acme/realms/acme"}):
        with pytest.raises(TenantExists):
            registry.create(actor="cli:op", **duplicate)
    with registry.engine.connect() as db:
        rows = db.execute(select(admin_audit).where(admin_audit.c.tenant == "acme")).mappings().all()
    assert [(r["event"], r["actor"], r["outcome"]) for r in rows] == [("tenant_created", "cli:op", "succeeded")]
    assert rows[0]["detail"]["issuer"] == "https://sso.acme/realms/acme"


def test_only_active_tenants_pass_and_status_changes_are_audited(registry):
    registry.create("acme", actor="cli:op")
    registry.check("acme")
    with pytest.raises(TenantUnavailable) as unknown:
        registry.check("nobody")
    assert unknown.value.reason == "unknown"
    for status in (TenantStatus.SUSPENDED, TenantStatus.OFFBOARDING):
        registry.set_status("acme", status, actor="cli:op", reason="合同到期")
        with pytest.raises(TenantUnavailable) as inactive:
            registry.check("acme")
        assert inactive.value.reason == status
    registry.set_status("acme", TenantStatus.ACTIVE, actor="cli:op")
    registry.set_status("acme", TenantStatus.ACTIVE, actor="cli:op")   # no change, no entry
    registry.check("acme")
    with pytest.raises(KeyError):
        registry.set_status("nobody", TenantStatus.SUSPENDED, actor="cli:op")
    with registry.engine.connect() as db:
        details = db.execute(select(admin_audit.c.detail).where(admin_audit.c.event == "tenant_status_changed")
                             .order_by(admin_audit.c.id)).scalars().all()
    assert [(d["from"], d["to"]) for d in details] == [("active", "suspended"), ("suspended", "offboarding"), ("offboarding", "active")]


def test_lookups_are_cached_but_local_changes_take_effect_at_once(db_url):
    registry = TenantRegistry(shared_engine(db_url), cache_ttl_s=60)
    registry.create("acme", actor="cli:op")
    registry.check("acme")
    registry.set_status("acme", TenantStatus.SUSPENDED, actor="cli:op")
    with pytest.raises(TenantUnavailable):
        registry.check("acme")


@pytest.fixture
def http(registry, monkeypatch):
    monkeypatch.setattr(state, "tenants", registry)
    return TestClient(api.app)


def test_requests_need_a_registered_active_tenant(http, registry, monkeypatch):
    assert http.get("/auth/me", headers=auth("alice")).status_code == 200                        # "default" is seeded
    unknown = http.get("/auth/me", headers=auth("alice", tenant="nobody"))
    assert unknown.status_code == 401 and unknown.json()["detail"] == "租户未开通"
    registry.create("acme", actor="cli:op")
    assert http.get("/auth/me", headers=auth("alice", tenant="acme")).json()["tenant"] == "acme"
    registry.set_status("acme", TenantStatus.SUSPENDED, actor="cli:op")
    assert http.get("/auth/me", headers=auth("alice", tenant="acme")).status_code == 403


def test_registry_failure_refuses_the_request(http, registry, monkeypatch):
    def broken(tenant):
        raise ConnectionError("db down")
    monkeypatch.setattr(registry, "get", broken)
    assert http.get("/auth/me", headers=auth("alice")).status_code == 503




def test_cli_adds_suspends_and_lists(db_url, monkeypatch, capsys):
    import scripts.tenants as cli

    monkeypatch.setenv("SERVICEDESK_AGENT_DATABASE_URL", db_url)
    assert cli.main(["add", "acme", "--tier", "enterprise", "--quotas", '{"agent_turn": "600/m"}']) == 1   # malformed rate
    assert cli.main(["add", "acme", "--tier", "enterprise", "--quotas", '{"agent_turn": "600/60"}']) == 0
    assert cli.main(["add", "acme"]) == 1
    assert cli.main(["quotas", "acme", '{"daily_cost_usd": -1}']) == 1
    assert cli.main(["quotas", "acme", '{"daily_cost_usd": 50}']) == 0
    assert cli.main(["quotas", "ghost", '{}']) == 1
    assert cli.main(["set", "acme", "--issuer", "https://sso.acme/realms/acme"]) == 0
    assert cli.main(["set", "default", "--issuer", "https://sso.acme/realms/acme"]) == 1         # already bound to acme
    assert cli.main(["set", "ghost", "--issuer", "x"]) == 1
    assert cli.main(["suspend", "acme", "--reason", "合同到期"]) == 0
    assert cli.main(["activate", "ghost"]) == 1
    capsys.readouterr()
    assert cli.main(["list"]) == 0
    listed = capsys.readouterr().out.split("\n")
    assert any(line.split()[:3] == ["acme", "suspended", "enterprise"] for line in listed)


def test_bindings_can_change_later_and_are_audited(registry):
    registry.create("acme", actor="cli:op")
    registry.set_bindings("acme", actor="cli:op", issuer="https://acme.example")
    registry.set_bindings("acme", actor="cli:op", issuer="https://acme.example")    # unchanged: no entry
    assert "https://acme.example" in registry.issuers()
    registry.set_bindings("acme", actor="cli:op", issuer="")           # unbind
    assert "https://acme.example" not in registry.issuers()
    with pytest.raises(ValueError):
        registry.set_bindings("acme", actor="cli:op", status="active")
    with registry.engine.connect() as db:
        details = db.execute(select(admin_audit.c.detail).where(admin_audit.c.event == "tenant_bindings_changed")
                             .order_by(admin_audit.c.id)).scalars().all()
    assert details == [{"from": {"issuer": None}, "to": {"issuer": "https://acme.example"}},
                       {"from": {"issuer": "https://acme.example"}, "to": {"issuer": None}}]
