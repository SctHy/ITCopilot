"""Knowledge write endpoints, admin token guard, offline evaluation report endpoint; no lifespan, no network."""
import json

import pytest
from fastapi.testclient import TestClient

import api.main as api
from tooling.tool_manager import MCPToolManager


class FakeKnowledgeBase:
    def __init__(self):
        self.documents = []

    async def add_documents_async(self, documents):
        for doc in documents:
            if not doc["content"].strip():
                raise ValueError("文档标题和内容不能为空")
        self.documents.extend(documents)
        return len(documents)

    async def doc_count_async(self):
        return len(self.documents)


@pytest.fixture
def client(monkeypatch):
    kb = FakeKnowledgeBase()
    monkeypatch.setattr(api, "_kb", kb)
    monkeypatch.setattr(api, "_tool_manager", MCPToolManager("sk-local"))
    monkeypatch.delenv("IT_COPILOT_ADMIN_TOKEN", raising=False)
    return TestClient(api.app), kb


def test_knowledge_add_imports_documents_and_invalidates_search_cache(client):
    http, kb = client
    version = api._tool_manager._cache_versions.get("knowledge_search", 0)
    response = http.post("/knowledge/add", json={"documents": [{"title": "打印机 SOP", "content": "先检查驱动与队列", "department": "研发"}]})
    assert response.status_code == 200 and response.json()["documents"] == 1
    assert kb.documents == [{"title": "打印机 SOP", "content": "先检查驱动与队列", "department": "研发"}]
    assert api._tool_manager._cache_versions["knowledge_search"] == version + 1


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


def test_admin_token_guards_write_endpoints(client, monkeypatch):
    http, _ = client
    monkeypatch.setenv("IT_COPILOT_ADMIN_TOKEN", "s3cret")
    doc = {"documents": [{"title": "t", "content": "c"}]}
    assert http.post("/knowledge/add", json=doc).status_code == 401
    assert http.post("/knowledge/add", json=doc, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert http.post("/knowledge/add", json=doc, headers={"Authorization": "Bearer s3cret"}).status_code == 200
    assert http.post("/approvals/a1", json={"decision": "approved"}).status_code == 401
    assert http.post("/skills/reload").status_code == 401
    assert http.get("/knowledge/stats").status_code == 200          # 只读接口不受影响


def test_eval_reports_summarize_committed_offline_reports(client):
    http, _ = client
    body = http.get("/eval/reports").json()
    assert body["gate"] == {"status": "passed", "all_passed": True}
    suites = {suite["id"]: suite for suite in body["suites"]}
    assert set(suites) == {"intent", "calibration", "rag", "memory", "planner", "safety"}
    assert all(suite["status"] == "passed" and suite["metrics"] for suite in suites.values())
    rag = {m["label"]: m["value"] for m in suites["rag"]["metrics"]}
    assert rag["Hybrid Recall@5"] == 1.0 and rag["Vector Recall@5"] == 0.8
