"""Administrative changes leave an append-only, tenant-scoped trail readable by admins and auditors."""
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
