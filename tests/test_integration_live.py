"""Opt-in integration tests against real services (skipped unless configured).

    docker compose up -d keycloak gitea gitea-init gitea-bootstrap   # or standalone containers
    IT_COPILOT_TEST_OIDC_ISSUER=http://127.0.0.1:8180/realms/it-copilot \
    IT_COPILOT_TEST_GITEA_URL=http://127.0.0.1:3001 IT_COPILOT_TEST_GITEA_PASSWORD=... \
    pytest tests/test_integration_live.py
"""
import asyncio
import os

import httpx
import pytest

from adapters import AdapterRegistry
from adapters.gitea import GiteaAdapter
from adapters.local import LocalDirectoryAdapter
from core.identity import AuthError, principal_from_token
from tests.test_workflow import ALICE, BOB, Clock, access
from workflow.store import WorkflowStore
from workflow.worker import ExecutionWorker

ISSUER = os.getenv("IT_COPILOT_TEST_OIDC_ISSUER")
GITEA = os.getenv("IT_COPILOT_TEST_GITEA_URL")


def keycloak_token(username):
    response = httpx.post(f"{ISSUER}/protocol/openid-connect/token", data={
        "grant_type": "password", "client_id": "it-copilot-web", "username": username, "password": f"{username}-pass"})
    response.raise_for_status()
    return response.json()["access_token"]


@pytest.mark.skipif(not ISSUER, reason="IT_COPILOT_TEST_OIDC_ISSUER not set")
def test_keycloak_tokens_map_to_principals(monkeypatch):
    monkeypatch.setenv("IT_COPILOT_AUTH_MODE", "oidc")
    monkeypatch.setenv("OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("OIDC_AUDIENCE", "it-copilot")
    bob = principal_from_token(keycloak_token("bob"))
    assert (bob.username, bob.employee_id, bob.department, bob.roles) == ("bob", "2001", "engineering", frozenset({"employee", "approver"}))
    token = keycloak_token("alice")
    head, body, sig = token.split(".")
    with pytest.raises(AuthError):
        principal_from_token(f"{head}.{body}.{sig[:-4]}AAAA")
    monkeypatch.setenv("OIDC_AUDIENCE", "another-service")
    with pytest.raises(AuthError, match="audience"):
        principal_from_token(token)


@pytest.mark.skipif(not ISSUER, reason="IT_COPILOT_TEST_OIDC_ISSUER not set")
def test_http_api_accepts_keycloak_and_rejects_dev_tokens(monkeypatch):
    from fastapi.testclient import TestClient

    import api.main as api
    from tests.conftest import auth

    monkeypatch.setenv("IT_COPILOT_AUTH_MODE", "oidc")
    monkeypatch.setenv("OIDC_ISSUER", ISSUER)
    client = TestClient(api.app)
    me = client.get("/auth/me", headers={"Authorization": "Bearer " + keycloak_token("grace")}).json()
    assert me["roles"] == ["auditor"] and me["department"] == "security"
    monkeypatch.setenv("IT_COPILOT_AUTH_MODE", "dev")
    dev = auth("erin")
    monkeypatch.setenv("IT_COPILOT_AUTH_MODE", "oidc")
    assert client.get("/auth/me", headers=dev).status_code == 401


@pytest.mark.skipif(not GITEA, reason="IT_COPILOT_TEST_GITEA_URL not set")
def test_real_gitea_grant_expiry_and_drift(tmp_path):
    clock = Clock()
    store = WorkflowStore(f"sqlite:///{tmp_path / 'wf.sqlite3'}", clock=clock, route=lambda a: "gitea" if a["tool_name"] == "request_access" else "local")
    gitea = GiteaAdapter(GITEA, "", {"gitlab": "it-sandbox/platform", "jira": "it-sandbox/platform"}, store.username_of,
                         basic_auth=(os.getenv("IT_COPILOT_TEST_GITEA_USER", "itc-svc"), os.environ["IT_COPILOT_TEST_GITEA_PASSWORD"]))
    worker = ExecutionWorker(store, AdapterRegistry(LocalDirectoryAdapter(store.engine), {"gitlab": gitea, "jira": gitea}), worker_id="live", base_delay_s=0)

    async def run():
        # Clean slate for alice on the sandbox repo.
        await gitea._request("DELETE", "/repos/it-sandbox/platform/collaborators/alice")
        pending = store.create("live", request_id="r", conv_id="c", principal=ALICE, action_requests=[access(role="readonly", duration_hours=1)])
        store.decide("live", "approved", BOB, pending["payload_hash"])
        await worker.run_until_idle()
        assert store.get("live")["execution"] == "succeeded"
        assert await gitea.current_level("1001", "gitlab") == "readonly"
        clock.t += 3601
        await worker.run_until_idle()
        assert await gitea.current_level("1001", "gitlab") is None
        assert store.employee("1001")["access_ledger"][0]["status"] == "revoked"
        # Drift: an operator upgrades alice by hand after our grant; expiry must not delete it.
        drift = store.create("drift", request_id="r2", conv_id="c", principal=ALICE, action_requests=[access(role="readonly", duration_hours=1)])
        store.decide("drift", "approved", BOB, drift["payload_hash"])
        await worker.run_until_idle()
        await gitea._request("PUT", "/repos/it-sandbox/platform/collaborators/alice", json={"permission": "write"})
        clock.t += 3601
        await worker.run_until_idle()
        assert await gitea.current_level("1001", "gitlab") == "developer"
        assert store.get("drift")["revocations"][0]["status"] == "manual_review"
        await gitea._request("DELETE", "/repos/it-sandbox/platform/collaborators/alice")
        await gitea.close()

    asyncio.run(run())
