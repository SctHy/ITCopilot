"""Tenant scope, issuer binding and per-tenant sandbox directories (ADR 0023)."""
import asyncio
from contextlib import nullcontext
from dataclasses import replace

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from api import state
from api.deps import current_principal
from core import identity
from core.db import shared_engine
from core.identity import AuthError
from core.tenancy import TenantRegistry, TenantStatus
from core.tenant_context import SYSTEM, TenantContextMissing, current_tenant, system_scope, tenant_scope
from intake.feishu import bind_workspace
from tests.conftest import auth
from tests.test_workflow import ALICE, BOB, access, env  # noqa: F401  (fixture)
from workflow.adapters.local import LocalDirectoryAdapter

ACME_ALICE, ACME_BOB = replace(ALICE, tenant="acme"), replace(BOB, tenant="acme")


@pytest.fixture
def registry(db_url):
    registry = TenantRegistry(shared_engine(db_url), cache_ttl_s=0)
    registry.create("acme", actor="cli:op")
    return registry


# ── Request scope ────────────────────────────────────────────────────────────

def test_the_callers_tenant_is_in_scope_for_async_and_sync_endpoints_and_their_threads():
    app = FastAPI()

    @app.get("/async")
    async def scoped_async(_=Depends(current_principal)):
        return {"here": current_tenant(), "thread": await asyncio.to_thread(current_tenant)}

    @app.get("/sync")
    def scoped_sync(_=Depends(current_principal)):
        return {"here": current_tenant()}

    client = TestClient(app)
    assert client.get("/async", headers=auth("alice", tenant="acme")).json() == {"here": "acme", "thread": "acme"}
    assert client.get("/sync", headers=auth("alice", tenant="globex")).json() == {"here": "globex"}
    assert current_tenant() is None   # the test thread never entered one


def test_scopes_nest_and_restore():
    assert current_tenant() is None
    with system_scope():
        with tenant_scope("acme"):
            assert current_tenant() == "acme"
        assert current_tenant() == SYSTEM
    assert current_tenant() is None


# ── Identity ─────────────────────────────────────────────────────────────────

def test_tokens_must_name_their_tenant():
    token = auth("alice", tenant="")["Authorization"].split()[1]
    with pytest.raises(AuthError, match="no tenant claim"):
        identity.principal_from_token(token)


def test_a_token_only_speaks_for_the_tenant_bound_to_its_issuer(registry, monkeypatch):
    registry.create("globex", actor="cli:op", issuer="https://sso.globex.example/realms/globex")
    monkeypatch.setattr(state, "tenants", registry)
    app = FastAPI()
    app.get("/me")(lambda principal=Depends(current_principal): principal.tenant)
    client = TestClient(app)
    assert client.get("/me", headers=auth("alice", tenant="acme")).json() == "acme"     # no own realm: the default issuer
    refused = client.get("/me", headers=auth("alice", tenant="globex"))                 # dev issuer claiming globex
    assert refused.status_code == 401 and refused.json()["detail"] == "令牌签发方与租户不匹配"


def test_oidc_trusts_only_configured_and_tenant_issuers_before_fetching_keys(monkeypatch):
    monkeypatch.setenv("IT_COPILOT_AUTH_MODE", "oidc")
    monkeypatch.setenv("OIDC_ISSUER", "https://sso.example/realms/main")
    fetched = []

    class Keys:
        def __init__(self, url):
            fetched.append(url)

        def get_signing_key_from_jwt(self, token):
            raise jwt.PyJWKClientError("stop here")
    monkeypatch.setattr(identity, "_jwks_client", Keys)

    def token(iss):
        return jwt.encode({"iss": iss, "sub": "u", "exp": 4102444800, "iat": 1}, "x" * 32, algorithm="HS256")
    with pytest.raises(AuthError, match="untrusted token issuer"):
        identity._oidc_claims(token("https://evil.example/realms/x"))
    assert fetched == []                                                   # never asked the attacker for keys
    monkeypatch.setattr(identity, "extra_issuers", lambda: {"https://sso.acme.example/realms/acme"})
    with pytest.raises(AuthError):
        identity._oidc_claims(token("https://sso.acme.example/realms/acme"))
    assert fetched == ["https://sso.acme.example/realms/acme/protocol/openid-connect/certs"]


def test_feishu_people_count_only_from_their_tenants_workspace(registry, db_url):
    registry.create("globex", actor="cli:op", feishu_tenant_key="tk-globex")
    with registry.engine.begin() as db:
        from sqlalchemy import update

        from core.db import tenants
        db.execute(update(tenants).where(tenants.c.id == "acme").values(feishu_tenant_key="tk-acme"))
    resolve = bind_workspace(lambda sender: ACME_ALICE if sender.get("user_id") == "alice" else None, registry)
    assert resolve({"user_id": "alice", "tenant_key": "tk-acme"}) == ACME_ALICE
    assert resolve({"user_id": "alice", "tenant_key": "tk-globex"}) is None      # mapped, but from another workspace
    assert resolve({"user_id": "alice"}) is None
    assert resolve({"user_id": "mallory", "tenant_key": "tk-acme"}) is None


# ── Directories and the worker ───────────────────────────────────────────────

def test_each_tenant_has_its_own_directory_and_actions_land_in_it(env):  # noqa: F811
    store, worker, _ = env
    store.seed_directory("acme")
    pending = store.create("acme-1", request_id="r", conv_id="c", principal=ACME_ALICE, action_requests=[access()])
    store.decide("acme-1", "approved", ACME_BOB, pending["payload_hash"])
    asyncio.run(worker.run_until_idle())
    assert store.get("acme-1")["execution"] == "succeeded"
    assert [g["role"] for g in store.employee("1001", "acme")["grants"]] == ["readonly"]
    assert store.employee("1001", "default")["grants"] == []                       # same id, other tenant: untouched
    assert store.employee("1001", "globex") is None                                 # no directory at all
    assert {g["status"] for g in store.employee("1001", "acme")["access_ledger"]} == {"active"}
    assert store.employee("1001", "default")["access_ledger"] == []


def test_an_action_for_a_tenant_without_that_employee_fails_instead_of_using_another_tenants(env):  # noqa: F811
    store, worker, _ = env
    globex = replace(ALICE, tenant="globex")
    pending = store.create("g-1", request_id="r", conv_id="c", principal=globex, action_requests=[access()])
    store.decide("g-1", "approved", replace(BOB, tenant="globex"), pending["payload_hash"])
    asyncio.run(worker.run_until_idle())
    assert store.get("g-1")["execution"] == "failed"
    assert store.employee("1001", "default")["grants"] == []


def test_the_sandbox_directory_refuses_work_outside_a_tenant_scope(env):  # noqa: F811
    store, _, _ = env
    adapter = LocalDirectoryAdapter(store.engine)
    for scope in (nullcontext(), system_scope()):
        with scope:
            with pytest.raises(TenantContextMissing):
                asyncio.run(adapter.current_level("1001", "gitlab"))
            with pytest.raises(TenantContextMissing):
                store.username_of("1001")


def test_the_worker_leaves_a_suspended_tenants_actions_queued(env, registry):  # noqa: F811
    store, worker, _ = env
    store.seed_directory("acme")
    pending = store.create("acme-1", request_id="r", conv_id="c", principal=ACME_ALICE, action_requests=[access()])
    registry.set_status("acme", TenantStatus.SUSPENDED, actor="cli:op")
    store.decide("acme-1", "approved", ACME_BOB, pending["payload_hash"])
    asyncio.run(worker.run_until_idle())
    assert store.get("acme-1")["execution"] == "queued" and store.employee("1001", "acme")["grants"] == []
    registry.set_status("acme", TenantStatus.ACTIVE, actor="cli:op")
    asyncio.run(worker.run_until_idle())
    assert store.get("acme-1")["execution"] == "succeeded"


@pytest.mark.parametrize("status", [TenantStatus.SUSPENDED, TenantStatus.OFFBOARDING])
def test_inactive_tenant_still_loses_expired_access(env, registry, status):  # noqa: F811
    store, worker, clock = env
    store.seed_directory("acme")
    pending = store.create("expiring", request_id="expiring", conv_id="c", principal=ACME_ALICE,
                           action_requests=[access(duration_hours=1)])
    store.decide("expiring", "approved", ACME_BOB, pending["payload_hash"])
    asyncio.run(worker.run_until_idle())
    assert store.employee("1001", "acme")["grants"]
    queued = store.create("new-access", request_id="new-access", conv_id="other", principal=ACME_ALICE,
                          action_requests=[access(system="Jira")])
    store.decide("new-access", "approved", ACME_BOB, queued["payload_hash"])
    registry.set_status("acme", status, actor="cli:op")
    clock.t += 3601
    asyncio.run(worker.run_until_idle())
    assert store.employee("1001", "acme")["grants"] == []
    assert store.get("expiring")["revocations"][0]["status"] == "succeeded"
    assert store.get("new-access")["execution"] == "queued"


def test_the_deployments_own_target_systems_act_only_for_the_operator_tenant(env, monkeypatch):  # noqa: F811
    from workflow.adapters import AdapterRegistry
    from workflow.worker import build_registry

    store, _, _ = env
    local, gitea = LocalDirectoryAdapter(store.engine), object()
    registry = AdapterRegistry(local, {"gitlab": gitea}, shared_with={"default"})
    grant = access()["arguments"] | {"system": "gitlab"}
    with tenant_scope("default"):
        assert registry.for_action({"tool_name": "request_access", "arguments": grant}) is gitea
    with tenant_scope("acme"):
        assert registry.for_action({"tool_name": "request_access", "arguments": grant}) is local
    assert build_registry(store).shared_with == {"default"}
    monkeypatch.setenv("IT_COPILOT_PLATFORM_TENANT", "ops")
    assert build_registry(store).shared_with == {"ops"}
