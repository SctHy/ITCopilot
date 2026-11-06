"""Vector store contract, run on SQLite (numpy similarity) and, when configured, PostgreSQL + pgvector.

Set IT_COPILOT_TEST_DATABASE_URL to a pgvector-enabled PostgreSQL to run the same cases there.
"""
import numpy as np
import pytest

from core.vector_store import VectorStore

DOCS = {
    "vpn": ("VPN 客户端证书过期导致无法连接，需重新下载证书", {"department": "*", "user_id": "alice"}),
    "erp": ("ERP 报销权限需要财务负责人审批", {"department": "财务", "user_id": "bob"}),
    "wifi": ("会议室 Wi-Fi 无法上网，检查 DNS 与 SSID", {"department": "研发", "user_id": "alice"}),
}


@pytest.fixture
def collection(db_url):
    store = VectorStore(db_url)
    kb = store.collection("kb")
    kb.upsert(list(DOCS), [d for d, _ in DOCS.values()], [m for _, m in DOCS.values()])
    yield kb
    store.dispose()


def test_query_ranks_semantic_match_first_with_cosine_distance(collection):
    result = collection.query(["公司 VPN 连不上"], n_results=3)
    assert result["ids"][0][0] == "vpn"
    distances = result["distances"][0]
    assert distances == sorted(distances) and 0 <= distances[0] < 1
    assert result["metadatas"][0][0] == {"department": "*", "user_id": "alice"}


def test_where_filters_are_applied_before_ranking(collection):
    finance = collection.query(["VPN 证书"], n_results=3, where={"department": {"$in": ["*", "财务"]}})
    assert set(finance["ids"][0]) == {"vpn", "erp"}
    alice_rd = collection.query(["网络"], n_results=3, where={"$and": [{"user_id": "alice"}, {"department": "研发"}]})
    assert alice_rd["ids"][0] == ["wifi"]
    assert collection.query(["网络"], n_results=3, where={"user_id": "nobody"})["ids"] == [[]]
    with pytest.raises(TypeError):
        collection.query(["x"], where={"importance": 0.5})


def test_upsert_replaces_and_delete_removes(collection):
    collection.upsert(["erp"], ["ERP 采购权限申请流程"], [{"department": "财务"}])
    assert collection.count() == 3
    assert collection.get(ids=["erp"])["documents"] == ["ERP 采购权限申请流程"]
    assert collection.get(where={"department": "财务"})["ids"] == ["erp"]
    collection.delete(["erp", "missing"])
    assert collection.count() == 2 and collection.get(ids=["erp"])["ids"] == []
    with pytest.raises(ValueError):
        collection.upsert(["a", "a"], ["x", "y"])


def test_collections_are_isolated(collection, db_url):
    store = VectorStore(db_url)
    other = store.collection("memory")
    assert other.count() == 0 and other.query(["VPN"], n_results=3)["ids"] == [[]]
    store.dispose()


def test_model_change_reembeds_stale_rows(collection, db_url):
    class Fake:
        model_name = "fake-model"

        def embed_documents(self, texts):
            return [np.ones(4, dtype=np.float32) / 2 for _ in texts]

        def embed_query(self, text):
            return np.ones(4, dtype=np.float32) / 2

    store = VectorStore(db_url, embedder=Fake())
    reopened = store.collection("kb")
    assert reopened.count() == 3
    # 旧模型（512 维）的向量已按新模型重算，查询不会混用两个向量空间。
    assert reopened.query(["任意"], n_results=5)["distances"][0] == pytest.approx([0.0, 0.0, 0.0], abs=1e-6)
    assert reopened.reembed_stale() == 0
    store.dispose()


def test_sqlite_reads_do_not_wait_for_an_open_write_transaction(tmp_path):
    import threading
    import time

    from sqlalchemy import text

    from core.db import make_engine, read

    engine = make_engine(f"sqlite:///{tmp_path / 'locks.sqlite3'}")
    with engine.begin() as db:
        db.execute(text("CREATE TABLE t (x INTEGER)"))
    writer_holds, release = threading.Event(), threading.Event()

    def writer():
        with engine.begin() as db:              # BEGIN IMMEDIATE：持有写锁直到提交
            db.execute(text("INSERT INTO t VALUES (1)"))
            writer_holds.set()
            release.wait(5)

    thread = threading.Thread(target=writer)
    thread.start()
    writer_holds.wait(5)
    started = time.monotonic()
    with read(engine) as db:                    # DEFERRED：WAL 下读到提交前的快照，不排队
        assert db.execute(text("SELECT count(*) FROM t")).scalar() == 0
    assert time.monotonic() - started < 1
    threading.Timer(0.5, release.set).start()
    started = time.monotonic()
    with engine.begin() as db:                  # 第二个写事务仍要等第一个提交
        db.execute(text("INSERT INTO t VALUES (2)"))
    assert time.monotonic() - started >= 0.4
    thread.join()
    engine.dispose()


def test_query_sees_writes_after_a_cached_query(collection):
    collection.query(["VPN"], n_results=3)
    collection.upsert(["printer"], ["打印机卡纸，需要清理进纸通道"], [{"department": "*"}])
    assert collection.query(["打印机卡纸"], n_results=1)["ids"][0] == ["printer"]
    collection.delete(["printer"])
    assert "printer" not in collection.query(["打印机卡纸"], n_results=3)["ids"][0]


def test_not_equal_filter_excludes_value_and_keeps_rows_missing_the_field(db_url):
    store = VectorStore(db_url)
    memories = store.collection("episodic")
    memories.upsert(["a1", "b1", "legacy"], ["VPN 报错 691", "VPN 证书过期", "VPN 旧记录"],
                    [{"user_id": "u", "conv_id": "a"}, {"user_id": "u", "conv_id": "b"}, {"user_id": "u"}])
    others = memories.query(["VPN"], n_results=5, where={"$and": [{"user_id": "u"}, {"conv_id": {"$ne": "a"}}]})
    assert set(others["ids"][0]) == {"b1", "legacy"}
    with pytest.raises(TypeError):
        memories.query(["VPN"], n_results=5, where={"conv_id": {"$ne": 1}})
    store.dispose()
