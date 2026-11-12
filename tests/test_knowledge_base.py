"""KnowledgeBase behaviour: scores per mode, batch metadata consistency, lexical index refresh policy."""
import math

import pytest

from tooling.knowledge_base import KnowledgeBase


@pytest.fixture
def kb(tmp_path, monkeypatch):
    monkeypatch.setenv("IT_COPILOT_SEED_DEMO_DOCS", "false")
    base = KnowledgeBase(f"sqlite:///{tmp_path / 'kb.sqlite3'}")
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


def test_lexical_index_refreshes_after_external_write(kb):
    # 绕过 add_documents 直接写集合，模拟其他进程写入同一数据库。
    kb._collection.add(ids=["external:0"], documents=["打印机 卡纸 处理"], metadatas=[{"title": "外部写入", "department": "*"}])
    assert any(r["title"] == "外部写入" for r in kb.search("打印机 卡纸", mode="bm25"))   # 片段数变化
    kb._collection.upsert(ids=["external:0"], documents=["扫描仪 卡纸 处理"], metadatas=[{"title": "外部写入", "department": "*"}])
    assert any(r["title"] == "外部写入" for r in kb.search("扫描仪", mode="bm25"))        # 片段数不变、写入时间变化
    kb._collection.delete(["external:0"])
    assert not any(r["title"] == "外部写入" for r in kb.search("扫描仪", mode="bm25"))


def test_department_filter_hides_other_departments(kb):
    for mode in ("hybrid", "vector", "bm25"):
        assert all(r["department"] in {"研发", "*"} for r in kb.search("权限 VPN", mode=mode, department="研发"))


def test_department_filter_is_not_crowded_out_by_other_departments(kb):
    kb.add_documents([{"title": f"财务 VPN 证书 {i}", "content": f"VPN 客户端证书与 DNS 排障 财务版 {i}", "department": "财务"} for i in range(40)])
    rows = kb.search("VPN 客户端证书与 DNS 排障 财务版", top_k=3, department="研发", mode="vector")
    assert [r["title"] for r in rows] == ["VPN 指南"]



def test_bm25_matches_reference_formula_within_department(kb):
    kb.add_documents([{"title": "VPN 证书", "content": "VPN 证书过期，VPN 重新下载证书", "department": "研发"},
                      {"title": "公共 VPN", "content": "VPN 连接失败先看网络", "department": "*"}])
    allowed = {i for i, row in kb._lexical_index.items() if row["metadata"]["department"] in {"*", "研发"}}
    docs = {i: kb._tokens(kb._lexical_index[i]["document"]) for i in allowed}
    terms = set(kb._tokens("VPN 证书"))
    avgdl = sum(map(len, docs.values())) / len(docs)
    expected = {}
    for i, tokens in docs.items():
        score = 0.0
        for term in terms:
            df, tf = sum(term in t for t in docs.values()), tokens.count(term)
            if tf:
                idf = math.log(1 + (len(docs) - df + .5) / (df + .5))
                score += idf * (tf * 2.0) / (tf + 1.5 * (0.25 + 0.75 * len(tokens) / avgdl))
        if score:
            expected[i] = score
    assert kb._bm25("VPN 证书", allowed) == pytest.approx(expected)


def test_chunks_split_on_sentence_boundaries_and_keep_original_text(kb):
    text = "Connect your domain. Then verify DNS!\nOpen settings? " * 30 + "先确认账号。再检查 DNS！" * 30
    chunks = kb._chunk_text(text, chunk_size=120)
    assert all(len(c) <= 120 for c in chunks)
    assert all(c.endswith((".", "!", "?", "。", "！")) for c in chunks)
    assert "".join(text.split()) == "".join("".join(chunks).split())


def test_tokens_drop_trailing_punctuation_but_keep_inner_symbols():
    assert KnowledgeBase._tokens("Domain. see /settings, VPN-691 at 10.0.0.1.") == ["domain", "see", "settings", "vpn-691", "at", "10.0.0.1"]


def test_truncated_lexical_expansion_matches_full_rrf(kb):
    kb.add_documents([{"title": f"VPN {i}", "content": "VPN 连接 " * (i % 5 + 1) + f"证书 {i}", "department": "*"} for i in range(30)])

    def reference(query, top_k, mode):
        index, lexical = kb._lexical_index, kb._bm25(query)
        vector = kb._collection.query([query], n_results=min(top_k * 4, len(index))) if mode != "bm25" else {"ids": [[]], "distances": [[]]}
        merged = {}
        for rank, (doc_id, distance) in enumerate(zip(vector["ids"][0], vector["distances"][0]), 1):
            merged.setdefault(doc_id, {"vector_score": max(0.0, 1 - distance), "bm25_score": lexical.get(doc_id, 0.0), "rrf": 0.0})["rrf"] += 1 / (60 + rank)
        for rank, (doc_id, score) in enumerate(sorted(lexical.items(), key=lambda item: item[1], reverse=True), 1):
            merged.setdefault(doc_id, {"vector_score": 0.0, "bm25_score": score, "rrf": 0.0})["rrf"] += 1 / (60 + rank)
        key = {"hybrid": "rrf", "bm25": "bm25_score"}[mode]
        ranked = sorted((item for item in merged.items() if mode == "hybrid" or item[1][key] > 0), key=lambda item: item[1][key], reverse=True)
        return [doc_id for doc_id, _ in ranked[:top_k]]

    for mode in ("hybrid", "bm25"):
        for top_k in (1, 2, 5):
            assert [r["chunk_id"] for r in kb.search("VPN 连接 证书", top_k=top_k, mode=mode)] == reference("VPN 连接 证书", top_k, mode)


def test_demo_seeding_updates_its_own_docs_but_never_overwrites_admin_docs(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'seed.sqlite3'}"
    seed = tmp_path / "knowledge"
    (seed / "it").mkdir(parents=True)
    (seed / "it" / "access.md").write_text("# 权限申请流程\n新版演示正文\n", encoding="utf-8")
    (seed / "it" / "sla.md").write_text("# 工单优先级与 SLA\n新版 SLA 正文\n", encoding="utf-8")
    (seed / "it" / "vpn.md").write_text("# VPN 指南\n演示 VPN 正文 v1\n", encoding="utf-8")
    monkeypatch.setenv("IT_COPILOT_KNOWLEDGE_DIR", str(seed))
    monkeypatch.setenv("IT_COPILOT_SEED_DEMO_DOCS", "false")
    KnowledgeBase(url).add_documents([
        {"title": "权限申请流程", "content": "管理员维护的正式流程"},  # admin upload, source defaults to title
    ])
    monkeypatch.setenv("IT_COPILOT_SEED_DEMO_DOCS", "true")
    contents = lambda base: {r["title"]: r["content"] for r in base.search("流程 SLA VPN 正文", top_k=20, mode="bm25")}
    seeded = contents(KnowledgeBase(url))
    assert seeded["权限申请流程"] == "管理员维护的正式流程"
    assert seeded["工单优先级与 SLA"] == "新版 SLA 正文"
    assert seeded["VPN 指南"] == "演示 VPN 正文 v1"
    (seed / "it" / "vpn.md").write_text("# VPN 指南\n演示 VPN 正文 v2\n", encoding="utf-8")
    assert contents(KnowledgeBase(url))["VPN 指南"] == "演示 VPN 正文 v2"


def test_documents_are_listed_per_document_and_deleted_from_both_indexes(kb):
    kb.add_documents([{"title": "打印机", "content": "驱动。" * 400, "department": "研发"}])  # several chunks
    listed = {d["title"]: d for d in kb.list_documents()}
    assert set(listed) == {"VPN 指南", "ERP 权限", "打印机"}
    assert listed["打印机"]["chunks"] > 1 and listed["打印机"]["demo_seed"] is False
    assert [d["title"] for d in kb.list_documents(department="财务")] == ["ERP 权限"]

    removed = kb.delete_document(listed["VPN 指南"]["document_id"])
    assert removed == {"document_id": listed["VPN 指南"]["document_id"], "title": "VPN 指南", "source": "VPN 指南", "chunks": 1, "demo_seed": False}
    for mode in ("bm25", "vector", "hybrid"):
        assert all(r["title"] != "VPN 指南" for r in kb.search("VPN 客户端证书", mode=mode, top_k=10))
    assert {d["title"] for d in kb.list_documents()} == {"ERP 权限", "打印机"}
    assert kb.delete_document(listed["VPN 指南"]["document_id"]) is None


def test_deleting_a_demo_document_reports_that_seeding_brings_it_back(tmp_path, monkeypatch):
    seed = tmp_path / "knowledge"
    seed.mkdir()
    (seed / "vpn.md").write_text("# VPN 指南\n演示 VPN 正文\n", encoding="utf-8")
    monkeypatch.setenv("IT_COPILOT_KNOWLEDGE_DIR", str(seed))
    monkeypatch.setenv("IT_COPILOT_SEED_DEMO_DOCS", "true")
    url = f"sqlite:///{tmp_path / 'kb.sqlite3'}"
    base = KnowledgeBase(url)
    base.add_documents([{"title": "管理员文档", "content": "正文", "source": "../outside.md"}])
    (tmp_path / "outside.md").write_text("x", encoding="utf-8")
    listed = {d["title"]: d for d in base.list_documents()}
    assert listed["VPN 指南"]["demo_seed"] is True
    assert listed["管理员文档"]["demo_seed"] is False   # a source outside knowledge/ is never treated as a seed file
    assert base.delete_document(listed["VPN 指南"]["document_id"])["demo_seed"] is True
    assert "VPN 指南" in {d["title"] for d in KnowledgeBase(url).list_documents()}   # re-seeded on the next start
