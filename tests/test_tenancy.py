"""Tenant registry and the multi-tenant switch (ADR 0022)."""
import asyncio
import pathlib

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text

import api.main as api
from api import state
from core import tenancy
from core.db import admin_audit, shared_engine
from core.tenancy import TenantExists, TenantRegistry, TenantStatus, TenantUnavailable
from tests.conftest import auth

ROOT = pathlib.Path(__file__).resolve().parent.parent


def upgrade(url, revision):
    config = Config()
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.upgrade(config, revision)


@pytest.fixture
def registry(db_url):
    return TenantRegistry(shared_engine(db_url), cache_ttl_s=0)


def test_migration_registers_default_and_every_tenant_that_already_owns_data(db_url):
    upgrade(db_url, "0002")
    engine = create_engine(db_url)
    with engine.begin() as db:
        db.execute(text("INSERT INTO session_revocations (tenant, subject, revoked_before, actor, at) VALUES ('acme', 'u', 1, 'a', 1)"))
        db.execute(text("INSERT INTO admin_audit (tenant, actor, event, at) VALUES ('*', 'system', 'policy_loaded', 1)"))
    engine.dispose()
    upgrade(db_url, "head")
    rows = TenantRegistry(shared_engine(db_url)).list()
    assert [(r["id"], r["status"]) for r in rows] == [("acme", "active"), ("default", "active")]  # "*" is not a tenant


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


def test_multi_tenant_mode_refuses_sqlite(monkeypatch):
    tenancy.ensure_supported("sqlite:///x.db")                       # off: anything goes
    monkeypatch.setenv("IT_COPILOT_MULTI_TENANT", "true")
    with pytest.raises(RuntimeError, match="PostgreSQL"):
        tenancy.ensure_supported("sqlite:///x.db")
    tenancy.ensure_supported("postgresql+psycopg://db/it")


@pytest.fixture
def http(registry, monkeypatch):
    monkeypatch.setattr(state, "tenants", registry)
    return TestClient(api.app)


def test_requests_need_a_registered_active_tenant_when_the_switch_is_on(http, registry, monkeypatch):
    assert http.get("/auth/me", headers=auth("alice")).status_code == 200                        # "default" is seeded
    unknown = http.get("/auth/me", headers=auth("alice", tenant="nobody"))
    assert unknown.status_code == 401 and unknown.json()["detail"] == "租户未开通"
    registry.create("acme", actor="cli:op")
    assert http.get("/auth/me", headers=auth("alice", tenant="acme")).json()["tenant"] == "acme"
    registry.set_status("acme", TenantStatus.SUSPENDED, actor="cli:op")
    assert http.get("/auth/me", headers=auth("alice", tenant="acme")).status_code == 403
    monkeypatch.setattr(state, "tenants", None)                                                   # switch off
    assert http.get("/auth/me", headers=auth("alice", tenant="nobody")).status_code == 200


def test_registry_failure_refuses_the_request(http, registry, monkeypatch):
    def broken(tenant):
        raise ConnectionError("db down")
    monkeypatch.setattr(registry, "get", broken)
    assert http.get("/auth/me", headers=auth("alice")).status_code == 503


def test_im_turns_from_an_inactive_tenant_get_a_reply_not_the_model(registry, monkeypatch):
    from dataclasses import replace

    from api.routes import chat
    from tests.test_workflow import ALICE

    async def no_model(*args):
        raise AssertionError("model work started for an inactive tenant")
    monkeypatch.setattr(state, "tenants", registry)
    monkeypatch.setattr(chat, "_chat_entry", no_model)
    registry.create("acme", actor="cli:op")
    registry.set_status("acme", TenantStatus.SUSPENDED, actor="cli:op")
    for tenant in ("acme", "nobody"):
        reply = asyncio.run(chat._channel_turn(replace(ALICE, tenant=tenant), "VPN 连不上", "c", "m-" + tenant))
        assert reply["response"] == chat.TENANT_INACTIVE_REPLY and not reply["approval_required"]


def test_cli_adds_suspends_and_lists(db_url, monkeypatch, capsys):
    import scripts.tenants as cli

    monkeypatch.setenv("IT_COPILOT_DATABASE_URL", db_url)
    assert cli.main(["add", "acme", "--tier", "enterprise", "--quotas", '{"agent_turn": "600/m"}']) == 0
    assert cli.main(["add", "acme"]) == 1
    assert cli.main(["suspend", "acme", "--reason", "合同到期"]) == 0
    assert cli.main(["activate", "ghost"]) == 1
    capsys.readouterr()
    assert cli.main(["list"]) == 0
    listed = capsys.readouterr().out.split("\n")
    assert any(line.split()[:3] == ["acme", "suspended", "enterprise"] for line in listed)


def test_tenant_columns_are_backfilled_and_audit_stays_append_only(db_url):
    """Migration 0004 on existing data (ADR 0023)."""
    from sqlalchemy.exc import DatabaseError

    upgrade(db_url, "0003")
    engine = create_engine(db_url)
    with engine.begin() as db:
        db.execute(text("INSERT INTO approvals (id, request_id, requester, tenant, status, execution, payload_hash, policy_version, "
                        "approvals_required, payload, version, created_at, expires_at) VALUES "
                        "('ap', 'r', 'dev:alice', 'acme', 'pending', 'not_executed', 'h', 'v', 1, '{}', 0, 0, 0)"))
        db.execute(text("INSERT INTO audit (approval_id, event, actor, at) VALUES ('ap', 'created', 'x', 0), ('gone', 'created', 'x', 0)"))
        db.execute(text("INSERT INTO directory_employees (id, username, active, locked, password_version) VALUES ('1001', 'alice', 1, 0, 0)"))
    engine.dispose()
    upgrade(db_url, "head")
    engine = create_engine(db_url)
    with engine.connect() as db:
        assert dict(db.execute(text("SELECT approval_id, tenant FROM audit")).all()) == {"ap": "acme", "gone": "default"}
        assert db.execute(text("SELECT tenant, id FROM directory_employees WHERE id = '1001'")).all() == [("default", "1001")]
    with pytest.raises(DatabaseError, match="append-only"):
        with engine.begin() as db:
            db.execute(text("UPDATE audit SET actor = 'someone else'"))
    engine.dispose()
