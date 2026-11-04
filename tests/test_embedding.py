from core.embedding import Embedder


def _captured(model_name, monkeypatch):
    seen = []
    embedder = Embedder(model_name)
    monkeypatch.setattr(embedder, "_embed", lambda texts: seen.extend(texts) or [None] * len(texts))
    return embedder, seen


def test_bge_zh_prefixes_queries_only(monkeypatch):
    embedder, seen = _captured("BAAI/bge-small-zh-v1.5", monkeypatch)
    embedder.embed_documents(["VPN 排障"])
    embedder.embed_query("VPN 连不上")
    assert seen == ["VPN 排障", "为这个句子生成表示以用于检索相关文章：VPN 连不上"]


def test_e5_prefixes_both_sides(monkeypatch):
    embedder, seen = _captured("intfloat/multilingual-e5-large", monkeypatch)
    embedder.embed_documents(["VPN 排障"])
    embedder.embed_query("VPN 连不上")
    assert seen == ["passage: VPN 排障", "query: VPN 连不上"]


def test_unknown_model_has_no_prefix(monkeypatch):
    embedder, seen = _captured("jinaai/jina-embeddings-v2-base-zh", monkeypatch)
    embedder.embed_documents(["a"])
    embedder.embed_query("b")
    assert seen == ["a", "b"]
