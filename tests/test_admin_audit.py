"""Administrative changes leave an append-only, tenant-scoped trail readable by admins and auditors."""
import asyncio

import pytest
from fastapi.testclient import TestClient

import api.main as api
from api import state
from core.retention import RetentionPolicy, RetentionSweeper
from tests.conftest import auth
from tests.test_api_admin import FakeKnowledgeBase
from tooling.tool_manager import MCPToolManager
from workflow.store import WorkflowStore


@pytest.fixture
def http(db_url, monkeypatch):
    store = WorkflowStore(db_url)
    monkeypatch.setattr(state, "workflow", store)
    monkeypatch.setattr(state, "worker", None)
    monkeypatch.setattr(state, "kb", FakeKnowledgeBase())
    monkeypatch.setattr(state, "tool_manager", MCPToolManager("sk-local"))
    monkeypatch.setattr(state, "retention", RetentionSweeper(store.engine, RetentionPolicy()))
    return TestClient(api.app)


def log(http, who="grace", **params):
    response = http.get("/admin/audit", params=params, headers=auth(who))
    assert response.status_code == 200, response.text
    return response.json()


def test_knowledge_changes_are_attributed_and_failed_changes_leave_nothing(http):
    erin = auth("erin")
    http.post("/knowledge/add", json={"documents": [{"title": "旧 SOP", "content": "过时"}]}, headers=erin)
    http.post("/knowledge/upload", files={"file": ("vpn.md", "# VPN\n检查客户端".encode())}, headers=erin)
    assert http.delete("/knowledge/documents/旧 SOP", headers=erin).status_code == 200
    assert http.delete("/knowledge/documents/旧 SOP", headers=erin).status_code == 404     # nothing happened, nothing logged
    assert http.post("/knowledge/add", json={"documents": [{"title": "t", "content": "c"}]}, headers=auth("alice")).status_code == 403
    items = log(http)["items"]
    assert [(i["event"], i["actor"], i["subject"]) for i in items] == [
        ("knowledge_deleted", "dev:erin", "旧 SOP"), ("knowledge_added", "dev:erin", None), ("knowledge_added", "dev:erin", None)]
    assert items[0]["detail"] == {"title": "旧 SOP", "source": "旧 SOP", "chunks": 1, "demo_seed": False}
    assert items[1]["detail"]["via"] == "upload:vpn.md" and items[2]["detail"]["titles"] == ["旧 SOP"]


def test_operational_actions_are_logged(http):
    erin = auth("erin")
    assert http.post("/policy/revalidate", headers=erin).status_code == 200
    assert http.post("/ops/retention/run", headers=erin).status_code == 200
    assert http.post("/skills/reload", headers=erin).status_code == 200
    events = [i["event"] for i in log(http)["items"]]
    assert events == ["skills_reloaded", "retention_run", "policy_revalidated"]
    assert log(http, event="retention_run")["items"][0]["detail"] == {"channel_inbox": 0, "channel_outbox": 0, "episodic_memory": 0}


def test_only_admins_and_auditors_read_their_own_tenant(http):
    http.post("/policy/revalidate", headers=auth("erin"))
    assert http.get("/admin/audit", headers=auth("alice")).status_code == 403
    assert http.get("/admin/audit", headers=auth("bob")).status_code == 403          # approvers are not auditors
    assert len(log(http, "erin")["items"]) == 1
    assert log(http, "erin")["items"] == log(http, "grace")["items"]
    other = http.get("/admin/audit", headers=auth("grace", tenant="other")).json()
    assert other["items"] == []


def test_keyset_paging(http):
    for _ in range(5):
        http.post("/policy/revalidate", headers=auth("erin"))
    first = log(http, limit=2)
    second = log(http, limit=2, before=first["next_before"])
    third = log(http, limit=2, before=second["next_before"])
    ids = [i["id"] for page in (first, second, third) for i in page["items"]]
    assert ids == sorted(ids, reverse=True) and len(set(ids)) == 5 and third["next_before"] is None


# ── Write-ahead entries and database-level immutability (migration 0002) ─────

def test_failed_attempts_are_kept_but_listed_only_when_asked(http):
    erin = auth("erin")
    assert http.delete("/knowledge/documents/missing", headers=erin).status_code == 404
    assert log(http)["items"] == []                                        # nothing changed: not in the default list
    failed = log(http, outcome="failed")["items"]
    assert [(i["event"], i["subject"], i["outcome"]) for i in failed] == [("knowledge_deleted", "missing", "failed")]
    assert failed[0]["detail"]["error"] == "document not found"


def test_a_change_that_never_reported_back_shows_as_interrupted(http, monkeypatch):
    """A crash after the change ran but before its outcome was written must not erase it."""
    from core import admin_audit

    monkeypatch.setattr(admin_audit, "finish", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("process died")))
    erin = auth("erin")
    with pytest.raises(RuntimeError):
        http.post("/policy/revalidate", headers=erin)
    assert log(http)["items"] == []                                        # too recent to call it interrupted
    items = admin_audit.recent(state.workflow.engine, "default", now=10 ** 12)
    assert [(i["event"], i["outcome"], i["actor"]) for i in items] == [("policy_revalidated", "interrupted", "dev:erin")]


def test_a_change_whose_audit_cannot_be_written_does_not_run(http, monkeypatch):
    from core import admin_audit

    monkeypatch.setattr(admin_audit, "begin", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("audit unavailable")))
    with pytest.raises(RuntimeError):
        http.post("/knowledge/add", json={"documents": [{"title": "t", "content": "c"}]}, headers=auth("erin"))
    assert state.kb.list_documents(None) == []


@pytest.mark.parametrize("table", ["audit", "admin_audit"])
def test_audit_tables_refuse_update_and_delete(http, table):
    from sqlalchemy import text
    from sqlalchemy.exc import DatabaseError

    http.post("/policy/revalidate", headers=auth("erin"))
    engine = state.workflow.engine
    with engine.begin() as db:
        if table == "audit":
            db.execute(text("INSERT INTO audit (tenant, approval_id, event, actor, at) VALUES ('default', 'a', 'created', 'x', 0)"))
    for statement in (f"UPDATE {table} SET actor = 'someone else'", f"DELETE FROM {table}"):
        with pytest.raises(DatabaseError, match="append-only"):
            with engine.begin() as db:
                db.execute(text(statement))
    with engine.connect() as db:
        assert db.execute(text(f"SELECT count(*) FROM {table}")).scalar() >= 1


def test_the_effective_policy_is_recorded_once_and_shown_to_every_tenant(http):
    from core import admin_audit
    from workflow.adapters import AdapterRegistry
    from workflow.adapters.local import LocalDirectoryAdapter
    from workflow.worker import ExecutionWorker

    store = state.workflow
    for worker_id in ("w1", "w2"):                                          # two workers / a restart see the same file
        worker = ExecutionWorker(store, AdapterRegistry(LocalDirectoryAdapter(store.engine)), worker_id=worker_id)
        asyncio.run(worker._revalidate_on_policy_change())
    entries = [i for i in log(http)["items"] if i["event"] == "policy_loaded"]
    assert len(entries) == 1 and entries[0]["global"] and entries[0]["actor"] == admin_audit.SYSTEM_ACTOR
    assert entries[0]["subject"] == store.policy.version
    assert entries[0]["detail"]["fingerprint"] == store.policy.fingerprint
    other = http.get("/admin/audit", headers=auth("grace", tenant="other")).json()["items"]
    assert [i["event"] for i in other] == ["policy_loaded"]
