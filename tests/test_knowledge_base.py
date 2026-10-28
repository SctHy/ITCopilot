"""KnowledgeBase behaviour: scores per mode, batch metadata consistency, lexical index refresh policy."""
import pytest

from tooling import knowledge_base
from tooling.knowledge_base import KnowledgeBase


@pytest.fixture
def kb(tmp_path, monkeypatch):
    monkeypatch.setenv("IT_COPILOT_SEED_DEMO_DOCS", "false")
    base = KnowledgeBase(chroma_path=str(tmp_path))
    base.add_documents([
        {"title": "VPN 指南", "content": "VPN 客户端证书与 DNS 排障", "department": "研发"},
        {"title": "ERP 权限", "content": "ERP 权限审批流程", "department": "财务"},
    ])
    return base


@pytest.mark.parametrize("mode,field", [("bm25", "bm25_score"), ("vector", "vector_score")])
def test_score_field_matches_the_requested_mode(kb, mode, field):
    rows = kb.search("VPN 证书", mode=mode)
    assert rows and all(row["score"] == row[field] for row in rows)


def test_same_title_in_one_batch_keeps_metadata_of_the_stored_version(kb):
    kb.add_documents([
        {"title": "打印机", "content": "旧驱动说明", "source": "wiki/old", "department": "研发"},
        {"title": "打印机", "content": "新驱动说明", "department": "财务"},
    ])
    row = next(r for r in kb.search("打印机 驱动", mode="bm25") if r["title"] == "打印机")
    assert (row["content"], row["source"], row["department"]) == ("新驱动说明", "打印机", "财务")


def test_lexical_index_is_not_rebuilt_on_every_search(kb, monkeypatch):
    rebuilds = []
    original = kb._rebuild_lexical_index
    monkeypatch.setattr(kb, "_rebuild_lexical_index", lambda: (rebuilds.append(1), original())[1])
    for _ in range(5):
        kb.search("VPN")
    assert rebuilds == []


def test_lexical_index_refreshes_after_external_write_or_ttl(kb, monkeypatch):
    kb._collection.add(ids=["external:0"], documents=["打印机 卡纸 处理"], metadatas=[{"title": "外部写入", "department": "*"}])
    assert any(r["title"] == "外部写入" for r in kb.search("打印机 卡纸", mode="bm25"))   # 片段数变化触发重建
    kb._collection.update(ids=["external:0"], documents=["扫描仪 卡纸 处理"])
    monkeypatch.setattr(knowledge_base, "LEXICAL_INDEX_TTL_S", 0)
    assert any(r["title"] == "外部写入" for r in kb.search("扫描仪", mode="bm25"))        # 片段数不变的更新靠 TTL 刷新


def test_department_filter_hides_other_departments(kb):
    for mode in ("hybrid", "vector", "bm25"):
        assert all(r["department"] in {"研发", "*"} for r in kb.search("权限 VPN", mode=mode, department="研发"))


def test_department_filter_is_not_crowded_out_by_other_departments(kb):
    kb.add_documents([{"title": f"财务 VPN 证书 {i}", "content": f"VPN 客户端证书与 DNS 排障 财务版 {i}", "department": "财务"} for i in range(40)])
    rows = kb.search("VPN 客户端证书与 DNS 排障 财务版", top_k=3, department="研发", mode="vector")
    assert [r["title"] for r in rows] == ["VPN 指南"]
