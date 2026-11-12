"""Knowledge write endpoints, admin token guard, offline evaluation report endpoint; no lifespan, no network."""
import json

import pytest
from fastapi.testclient import TestClient

import api.main as api
from api import state
from tests.conftest import auth
from tooling.tool_manager import MCPToolManager


class FakeKnowledgeBase:
    def __init__(self):
        self.documents = []

    def collection_version(self, tenant):
        return float(len(self.documents))

    async def add_documents_async(self, documents, tenant=None):
        for doc in documents:
            if not doc["content"].strip():
                raise ValueError("文档标题和内容不能为空")
        self.documents.extend(documents)
        return len(documents)

    async def doc_count_async(self, tenant=None):
        return len(self.documents)

    def list_documents(self, department=None, tenant=None):
        return [{"document_id": d["title"], "title": d["title"], "chunks": 1} for d in self.documents
                if department is None or d.get("department", "*") == department]

    def delete_document(self, document_id, tenant=None):
        for doc in self.documents:
            if doc["title"] == document_id:
                self.documents.remove(doc)
                return {"document_id": document_id, "title": doc["title"], "source": doc["title"], "chunks": 1, "demo_seed": False}
        return None


@pytest.fixture
def client(monkeypatch, db_url):
    from workflow.store import WorkflowStore
    monkeypatch.setattr(state, "workflow", WorkflowStore(db_url))
    kb = FakeKnowledgeBase()
    monkeypatch.setattr(state, "kb", kb)
    monkeypatch.setattr(state, "tool_manager", MCPToolManager("sk-local"))
    return TestClient(api.app, headers=auth("erin")), kb


def test_knowledge_add_imports_documents_and_invalidates_search_cache(client):
    http, kb = client
    version = state.tool_manager._cache_versions.get("knowledge_search", 0)
    response = http.post("/knowledge/add", json={"documents": [{"title": "打印机 SOP", "content": "先检查驱动与队列", "department": "研发"}]})
    assert response.status_code == 200 and response.json()["documents"] == 1
    assert kb.documents == [{"title": "打印机 SOP", "content": "先检查驱动与队列", "department": "研发"}]
    assert state.tool_manager._cache_versions["knowledge_search"] == version + 1


def test_knowledge_add_rejects_empty_payload(client):
    http, _ = client
    assert http.post("/knowledge/add", json={"documents": []}).status_code == 422
    assert http.post("/knowledge/add", json={"documents": [{"title": "t", "content": "   "}]}).status_code == 422


@pytest.mark.parametrize("filename,body,expected_titles", [
    ("vpn-guide.md", "# VPN\n检查客户端版本".encode(), ["vpn-guide"]),
    ("docs.json", json.dumps([{"title": "A", "content": "a"}, {"title": "B", "content": "b"}]).encode(), ["A", "B"]),
    ("wrapped.json", json.dumps({"documents": [{"title": "C", "content": "c"}]}).encode(), ["C"]),
])
def test_knowledge_upload_supports_text_and_json(client, filename, body, expected_titles):
    http, kb = client
    response = http.post("/knowledge/upload", files={"file": (filename, body)})
    assert response.status_code == 200, response.text
    assert [doc["title"] for doc in kb.documents] == expected_titles


@pytest.mark.parametrize("filename,body,status", [("a.pdf", b"x", 415), ("a.json", b"{not json", 422), ("a.json", b'[{"title": "x"}]', 422), ("a.txt", "测试".encode("gbk"), 422)])
def test_knowledge_upload_rejects_bad_files(client, filename, body, status):
    http, _ = client
    assert http.post("/knowledge/upload", files={"file": (filename, body)}).status_code == status


def test_roles_guard_write_endpoints(client):
    http, _ = client
    doc = {"documents": [{"title": "t", "content": "c"}]}
    anonymous = TestClient(api.app)
    assert anonymous.post("/knowledge/add", json=doc).status_code == 401
    assert anonymous.post("/knowledge/add", json=doc, headers={"Authorization": "Bearer forged"}).status_code == 401
    assert http.post("/knowledge/add", json=doc, headers=auth("alice")).status_code == 403   # employee, not admin
    assert http.post("/knowledge/add", json=doc).status_code == 200                          # erin is it_admin
    assert http.post("/approvals/a1", json={"decision": "approved", "payload_hash": "0" * 64}, headers=auth("alice")).status_code == 403
    assert http.post("/skills/reload", headers=auth("bob")).status_code == 403
    assert anonymous.get("/knowledge/stats").status_code == 401          # 片段数按租户统计，匿名请求没有租户（ADR 0024）
    assert http.get("/knowledge/stats").status_code == 200


def test_dev_tokens_are_refused_in_production(client, monkeypatch):
    http, _ = client
    token = auth("erin")
    monkeypatch.setenv("IT_COPILOT_ENV", "production")
    assert http.post("/knowledge/add", json={"documents": [{"title": "t", "content": "c"}]}, headers=token).status_code == 401
    assert http.post("/auth/dev-token", json={"username": "erin"}).status_code == 404


def test_auth_me_reflects_token_not_request_body(client):
    http, _ = client
    me = http.get("/auth/me", headers=auth("alice")).json()
    assert me["subject"] == "dev:alice" and me["department"] == "engineering" and me["roles"] == ["employee"]
    token = http.post("/auth/dev-token", json={"username": "bob"}).json()["access_token"]
    assert http.get("/auth/me", headers={"Authorization": "Bearer " + token}).json()["roles"] == ["approver", "employee"]


def test_stats_reports_the_chunk_count_expected_by_the_frontend(client):
    http, kb = client
    kb.documents = [{"title": "a"}, {"title": "b"}]
    assert http.get("/knowledge/stats").json()["total_chunks"] == 2


@pytest.mark.parametrize("top_k", [9, 10, 20])
def test_search_accepts_the_full_advertised_range(client, top_k):
    from tooling.knowledge_base import KNOWLEDGE_SEARCH_SCHEMA
    from tooling.tool_manager import Tool
    http, _ = client
    state.tool_manager.register(Tool("knowledge_search", "search", lambda p, c: [{"title": "hit"}], KNOWLEDGE_SEARCH_SCHEMA))
    response = http.post("/search", params={"query": "VPN", "top_k": top_k})
    assert response.status_code == 200
    assert response.json() == [{"title": "hit"}]


def test_knowledge_documents_can_be_listed_and_deleted_by_admins_only(client):
    http, kb = client
    http.post("/knowledge/add", json={"documents": [{"title": "旧 SOP", "content": "过时", "department": "研发"}, {"title": "新 SOP", "content": "现行"}]})
    assert http.get("/knowledge/documents", headers=auth("alice")).status_code == 403
    assert http.delete("/knowledge/documents/旧 SOP", headers=auth("alice")).status_code == 403
    assert http.get("/knowledge/documents").json()["total"] == 2
    assert [d["title"] for d in http.get("/knowledge/documents?department=研发").json()["items"]] == ["旧 SOP"]
    version = state.tool_manager._cache_versions.get("knowledge_search", 0)
    response = http.delete("/knowledge/documents/旧 SOP")
    assert response.status_code == 200 and response.json()["total_chunks"] == 1
    assert state.tool_manager._cache_versions["knowledge_search"] == version + 1   # agents stop seeing it at once
    assert http.delete("/knowledge/documents/旧 SOP").status_code == 404
