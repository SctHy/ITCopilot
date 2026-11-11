"""Token revocation: a per-identity watermark refuses tokens issued before it, on every endpoint."""
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert

import api.main as api
from api import state
from core import identity
from core.db import task_identities
from core.identity import principal_from_token
from core.session_revocation import SessionRevocations
from tests.conftest import auth
from workflow.store import WorkflowStore


def token_at(monkeypatch, username, at, **overrides):
    """A dev token whose iat is ``at``."""
    with monkeypatch.context() as m:
        m.setattr(identity.time, "time", lambda: at)
        return auth(username, **overrides)


@pytest.fixture
def env(db_url, monkeypatch):
    store = WorkflowStore(db_url)
    now = time.time()
    # Revocations happen "30 s ago": PyJWT refuses tokens issued in the future, so a fresh
    # sign-in after the revocation is simply a token minted now.
    sessions = SessionRevocations(store.engine, clock=lambda: now - 30)
    monkeypatch.setattr(state, "workflow", store)
    monkeypatch.setattr(state, "sessions", sessions)
    return TestClient(api.app), store, sessions, now


def test_watermark_refuses_older_tokens_and_never_moves_back(db_url):
    store = WorkflowStore(db_url)
    clock = [1000.4]
    sessions = SessionRevocations(store.engine, clock=lambda: clock[0], cache_ttl_s=0)
    old = principal_from_token(auth("alice")["Authorization"].split()[1])
    old = type(old)(**{**old.__dict__, "issued_at": 999})
    assert not sessions.is_revoked(old)
    assert sessions.revoke("default", "dev:alice", actor="dev:erin") == 1000.0
    assert sessions.is_revoked(old)
    assert sessions.is_revoked(type(old)(**{**old.__dict__, "issued_at": 1000}))      # same second counts as before
    assert not sessions.is_revoked(type(old)(**{**old.__dict__, "issued_at": 1001}))  # signed in again afterwards
    assert sessions.is_revoked(type(old)(**{**old.__dict__, "issued_at": None}))      # cannot prove it is newer
    assert not sessions.is_revoked(type(old)(**{**old.__dict__, "tenant": "other", "issued_at": 999}))
    clock[0] = 500.0                                                                    # a process with a slow clock
    sessions.revoke("default", "dev:alice", actor="dev:erin")
    assert sessions.watermark("default", "dev:alice") == 1000.0


def test_other_processes_see_a_revocation_within_the_cache_window(db_url):
    store = WorkflowStore(db_url)
    here, there = SessionRevocations(store.engine, cache_ttl_s=0.2), SessionRevocations(store.engine, cache_ttl_s=0.2)
    assert there.watermark("default", "dev:alice") is None   # cached "not revoked"
    here.revoke("default", "dev:alice", actor="dev:erin")
    assert here.watermark("default", "dev:alice") is not None  # the revoking process sees it at once
    time.sleep(0.25)
    assert there.watermark("default", "dev:alice") is not None


def test_admin_revokes_a_person_and_old_tokens_stop_everywhere(env, monkeypatch):
    http, store, _, now = env
    old = token_at(monkeypatch, "alice", now - 60)
    assert http.get("/auth/me", headers=old).status_code == 200
    assert http.post("/admin/sessions/revoke", json={"subject": "dev:alice"}, headers=auth("bob")).status_code == 403
    response = http.post("/admin/sessions/revoke", json={"subject": "dev:alice", "reason": "离职"}, headers=token_at(monkeypatch, "erin", now - 60))
    assert response.status_code == 200 and response.json()["revoked_before"] == float(int(now - 30))
    for path in ("/auth/me", "/approvals"):
        denied = http.get(path, headers=old)
        assert denied.status_code == 401 and "撤销" in denied.json()["detail"]
    assert http.get("/auth/me", headers=auth("alice")).status_code == 200                            # a fresh sign-in works
    assert http.get("/auth/me", headers=token_at(monkeypatch, "bob", now - 60)).status_code == 200   # nobody else is affected
    audit = http.get("/admin/audit", headers=auth("grace")).json()["items"][0]
    assert (audit["event"], audit["subject"], audit["detail"]["reason"]) == ("sessions_revoked", "dev:alice", "离职")
    listed = http.get("/admin/sessions/revocations", headers=auth("grace")).json()["items"]
    assert [(r["subject"], r["actor"], r["reason"]) for r in listed] == [("dev:alice", "dev:erin", "离职")]


def test_revocation_by_username_and_tenant_isolation(env, monkeypatch):
    http, store, _, now = env
    with store.engine.begin() as db:
        db.execute(insert(task_identities).values(tenant="default", subject="kc-uuid-1", profile={"username": "alice"}))
    assert http.post("/admin/sessions/revoke", json={"username": "nobody"}, headers=auth("erin")).status_code == 404
    assert http.post("/admin/sessions/revoke", json={}, headers=auth("erin")).status_code == 422
    assert http.post("/admin/sessions/revoke", json={"username": "alice"}, headers=auth("erin")).json()["subject"] == "kc-uuid-1"
    # An admin of another tenant revoking the same subject does not touch this tenant.
    http.post("/admin/sessions/revoke", json={"subject": "dev:alice"}, headers=auth("erin", tenant="other"))
    assert http.get("/auth/me", headers=token_at(monkeypatch, "alice", now - 60)).status_code == 200


def test_sign_out_everywhere(env, monkeypatch):
    http, _, _, now = env
    phone, laptop = token_at(monkeypatch, "alice", now - 120), token_at(monkeypatch, "alice", now - 60)
    assert http.post("/auth/logout-all", headers=laptop).status_code == 200
    assert http.get("/auth/me", headers=phone).status_code == 401 and http.get("/auth/me", headers=laptop).status_code == 401


def test_lookup_failure_refuses_the_request(env, monkeypatch):
    http, *_ = env

    class Broken:
        def is_revoked(self, principal):
            raise ConnectionError("db down")

    monkeypatch.setattr(state, "sessions", Broken())
    assert http.get("/auth/me", headers=auth("alice")).status_code == 503
