"""
RAG 知识库：文档切片写入应用数据库的向量表（core/vector_store.py），BM25 + 向量两路召回后用 RRF 融合。

功能：
  1. 文档导入：将文本切片后写入向量表（本地中文 embedding 自动生成向量）
  2. 检索：vector / bm25 / hybrid 三种模式，按部门隔离
  3. 与 MCP 工具框架集成：作为 knowledge_search 工具的真实 handler

memory/ 的情景记忆与用户画像在同一张表的不同 collection 里，互不干扰。
"""
import asyncio
import hashlib
import json
import logging
import math
import os
import pathlib
import re
import threading
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import numpy as np

from core.vector_store import VectorStore
from core.protocols import CacheState, cache_state
from core.states import Protocol

# knowledge_search 工具的参数契约：应用注册与评测脚本共用。
KNOWLEDGE_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "minLength": 1},
        "top_k": {"type": "integer", "minimum": 1, "maximum": 20},
        "department": {"type": ["string", "null"]},
        "mode": {"type": "string", "enum": ["hybrid", "vector", "bm25"]},
    },
    "required": ["query"],
    "additionalProperties": False,
}

logger = logging.getLogger(__name__)


DOCUMENT_UPDATE = Protocol("document_update", "prepared", {
    "prepared": {"write": "writing"},
    "writing": {"commit": "committed"},
    "committed": {"index": "indexed"},
})


class KnowledgeBase:
    """
    RAG 知识库。向量由本地中英 embedding（core/embedding.py，默认 jina-embeddings-v2-base-zh）生成，
    存在 PostgreSQL（pgvector）或本地 SQLite，不调用外部 Embeddings API。
    """

    COLLECTION_NAME = "knowledge_base"

    def __init__(self, database_url: str):
        self._write_lock = threading.RLock()
        self._index_lock = threading.RLock()
        self._lexical_index: Dict[str, Dict[str, Any]] = {}
        self._doc_ids: List[str] = []
        self._lengths = np.zeros(0)
        self._postings: Dict[str, tuple] = {}
        self._mask_cache: Dict[Optional[str], np.ndarray] = {}
        self._indexed_version: tuple = (-1, 0.)
        # Docker 已预下载 embedding 模型，本地首次使用会下载。
        self._collection = VectorStore(database_url).collection(self.COLLECTION_NAME)

        # 非生产环境导入演示知识库：只写入新增或内容有变化的文档，已有数据库也能补齐。
        seed_demo = os.getenv("IT_COPILOT_SEED_DEMO_DOCS", "true").lower() in {"1", "true", "yes"}
        self._seeding_demo = os.getenv("IT_COPILOT_ENV", "development").lower() != "production" and seed_demo
        if self._seeding_demo:
            self._load_default_docs()
        self._rebuild_lexical_index()

    # ── 文档管理 ──────────────────────────────────────────────────────────────

    def add_documents(self, documents: List[Dict[str, str]]) -> int:
        """
        批量导入文档到知识库。

        documents 格式: [{"title": "...", "content": "...", "department"?, "source"?, "updated_at"?, "document_id"?}, ...]
        长文档会自动切片（每片 500 字）。同一文档重复导入时整体替换旧片段。
        """
        # 相同文档身份在同一批次只采用最后一个版本（内容与元数据一致地取自该版本），避免重复 ID。
        prepared: Dict[str, Dict[str, Any]] = {}
        for doc in documents:
            title, content = doc.get("title", ""), doc.get("content", "")
            if not isinstance(title, str) or not isinstance(content, str):
                raise ValueError("文档 title 和 content 必须是字符串")
            if not title.strip() or not content.strip():
                raise ValueError("文档标题和内容不能为空")
            explicit_id = doc.get("document_id")
            if explicit_id is not None and (not isinstance(explicit_id, str) or not explicit_id.strip()):
                raise ValueError("document_id 必须是非空字符串")
            identity = json.dumps(["explicit", explicit_id] if explicit_id else ["title", title], ensure_ascii=True)
            prepared[identity] = doc

        count = 0
        with self._write_lock:
            flow = DOCUMENT_UPDATE.start()
            flow.send("write")
            for identity, doc in prepared.items():
                count += self._upsert_document(identity, doc)
            flow.send("commit")
            self._rebuild_lexical_index()
            flow.send("index")
        logger.info("知识库写入 %s 个文档片段", count)
        return count

    def _upsert_document(self, identity: str, doc: Dict[str, Any]) -> int:
        title, content, explicit_id = doc["title"], doc["content"], doc.get("document_id")
        document_id = hashlib.sha256(identity.encode()).hexdigest()
        chunks = self._chunk_text(content, chunk_size=500)
        ids = [f"{document_id}:{i}" for i in range(len(chunks))]
        stale = self._existing_chunk_ids(document_id, title, explicit_id) - set(ids)
        # 元数据显式写全：混合检索据此做部门隔离，并生成可追溯的 citation。
        base = {
            "title": title, "document_id": document_id, "version": hashlib.sha256(content.encode()).hexdigest(),
            "total_chunks": len(chunks), "source": str(doc.get("source") or title), "department": str(doc.get("department") or "*"),
            "updated_at": str(doc.get("updated_at") or datetime.now(timezone.utc).isoformat()),
        }
        self._collection.upsert(ids=ids, documents=chunks, metadatas=[{**base, "chunk_index": i, "chunk_id": chunk_id} for i, chunk_id in enumerate(ids)], delete_ids=stale)
        return len(chunks)

    def _existing_chunk_ids(self, document_id: str, title: str, explicit_id: Optional[str]) -> set:
        """同一文档已有的片段 ID（含旧版 ID 方案写入的片段），用于替换时清理过期片段。"""
        existing = set(self._collection.get(where={"document_id": document_id})["ids"])
        # 仅迁移归属可验证的旧身份；旧显式 ID 与 title: 前缀冲突时保留旧记录供核验。
        if not explicit_id or not explicit_id.startswith("title:"):
            legacy_id = hashlib.sha256((explicit_id or f"title:{title}").encode()).hexdigest()
            previous = self._collection.get(where={"document_id": legacy_id})
            existing.update(i for i, meta in zip(previous["ids"], previous["metadatas"]) if (meta or {}).get("title") == title)
        if not explicit_id:
            # 兼容旧版只有 title/chunk_index 元数据的片段。
            legacy = self._collection.get(where={"title": title})
            existing.update(i for i, meta in zip(legacy["ids"], legacy["metadatas"]) if not (meta or {}).get("document_id"))
        return existing

    def list_documents(self, department: Optional[str] = None) -> List[Dict[str, Any]]:
        """按文档聚合片段，供管理员核对知识库内容；department 为 None 时列出全部部门。"""
        self._ensure_lexical_index(self._collection.version())
        documents: Dict[str, Dict[str, Any]] = {}
        for entry in self._lexical_index.values():
            meta = entry["metadata"]
            document_id = meta.get("document_id") or entry["id"].rsplit(":", 1)[0]
            if department is not None and meta.get("department", "*") != department:
                continue
            item = documents.setdefault(document_id, {
                "document_id": document_id, "title": meta.get("title", ""), "source": meta.get("source", ""),
                "department": meta.get("department", "*"), "updated_at": meta.get("updated_at", ""),
                "version": meta.get("version", ""), "chunks": 0, "demo_seed": self._is_demo_source(meta.get("source", "")),
            })
            item["chunks"] += 1
        return sorted(documents.values(), key=lambda d: (d["title"], d["document_id"]))

    def delete_document(self, document_id: str) -> Optional[Dict[str, Any]]:
        """删除一篇文档的全部片段并立即重建 BM25 索引；文档不存在时返回 None。"""
        with self._write_lock:
            found = self._collection.get(where={"document_id": document_id})
            if not found["ids"]:
                return None
            meta = found["metadatas"][0] or {}
            self._collection.delete(found["ids"])
            self._rebuild_lexical_index()
        source = meta.get("source", "")
        logger.info("知识库删除文档《%s》（%s 个片段）", meta.get("title", ""), len(found["ids"]))
        return {"document_id": document_id, "title": meta.get("title", ""), "source": source, "chunks": len(found["ids"]),
                # 演示种子文档在下次启动时会按 knowledge/ 目录重新导入（生产环境不导入演示文档）。
                "demo_seed": self._is_demo_source(source)}

    def _is_demo_source(self, source: str) -> bool:
        if not (self._seeding_demo and source.endswith(".md")):
            return False
        root = self._knowledge_root().resolve()
        path = (root / source).resolve()
        return path.is_relative_to(root) and path.is_file()

    async def add_documents_async(self, documents: List[Dict[str, str]]) -> int:
        """异步导入文档；embedding 与数据库访问是同步的，因此放入线程池执行。"""
        return await asyncio.to_thread(self.add_documents, documents)

    @staticmethod
    def _tokens(text: str) -> List[str]:
        """Small dependency-free tokenizer for BM25; keeps CJK bigrams and words."""
        text = str(text or "").lower()
        # 去掉词首尾的 . / -（句末标点、路径分隔），保留 vpn-691、10.0.0.1 这类内部符号。
        words = [w for w in (w.strip("./-") for w in re.findall(r"[a-z0-9_./-]+|[\u4e00-\u9fff]", text)) if w]
        cjk = [a + b for a, b in zip(words, words[1:]) if re.match(r"[\u4e00-\u9fff]", a) and re.match(r"[\u4e00-\u9fff]", b)]
        return words + cjk

    def _rebuild_lexical_index(self):
        with self._index_lock:
            # 先取版本再读数据：读取期间若有写入，下次检索版本不一致会再重建，不会漏掉。
            version = self._collection.version()
            data = self._collection.get()
            index, lists, lengths = {}, {}, []
            for pos, (doc_id, document, metadata) in enumerate(zip(data.get("ids", []), data.get("documents", []), data.get("metadatas", []))):
                tokens = self._tokens(document)
                index[doc_id] = {"id": doc_id, "document": document, "metadata": metadata or {}, "pos": pos}
                lengths.append(len(tokens))
                for term, tf in Counter(tokens).items():
                    lists.setdefault(term, []).append((pos, tf))
            # 倒排表存成 numpy 数组：高频词的倒排表有上万项，打分按数组运算而不是逐项循环。
            postings = {term: (np.array([p for p, _ in items], dtype=np.int64), np.array([tf for _, tf in items], dtype=np.float64))
                        for term, items in lists.items()}
            # 整体替换而不原地修改：检索线程拿到引用后不受并发重建影响。
            self._lexical_index, self._doc_ids, self._lengths = index, list(index), np.array(lengths, dtype=np.float64)
            self._postings, self._mask_cache = postings, {}
            self._indexed_version = version

    def _ensure_lexical_index(self, version: tuple) -> None:
        """集合版本（行数, 最近写入时间）变化时重建 BM25 索引；覆盖其他进程写入同一数据库的情况。"""
        if cache_state(True, version_matches=version == self._indexed_version) == CacheState.STALE:
            self._rebuild_lexical_index()

    def _bm25_array(self, query: str, mask: np.ndarray) -> np.ndarray:
        """倒排索引上的 BM25，按索引位置返回分数数组；n、avgdl、df 都在 mask 选中的片段内计算。"""
        with self._index_lock:
            lengths, postings = self._lengths, self._postings
        scores = np.zeros(len(lengths))
        n = int(mask.sum())
        terms = set(self._tokens(query))
        if not terms or not n: return scores
        avgdl = max(1, lengths[mask].sum() / n)
        for term in terms:
            positions, tf = postings.get(term, (None, None))
            if positions is None: continue
            keep = mask[positions]
            positions, tf = positions[keep], tf[keep]
            if not len(positions): continue
            idf = math.log(1 + (n - len(positions) + .5) / (len(positions) + .5))
            scores[positions] += idf * (tf * 2.0) / (tf + 1.5 * (0.25 + 0.75 * lengths[positions] / avgdl))
        return scores

    def _bm25(self, query: str, candidates: Optional[set] = None) -> Dict[str, float]:
        """片段 ID -> BM25 分数（只含正分）。"""
        with self._index_lock:
            doc_ids = self._doc_ids
        mask = np.array([candidates is None or doc_id in candidates for doc_id in doc_ids], dtype=bool)
        scores = self._bm25_array(query, mask)
        return {doc_ids[i]: float(scores[i]) for i in np.flatnonzero(scores > 0)}

    def _department_mask(self, department: Optional[str]) -> np.ndarray:
        with self._index_lock:
            index, cache = self._lexical_index, self._mask_cache
        mask = cache.get(department)
        if mask is None:
            mask = cache[department] = np.array(
                [not department or row["metadata"].get("department", "*") in {"*", department} for row in index.values()], dtype=bool)
        return mask

    def search(self, query: str, top_k: int = 5, *, department: Optional[str] = None,
               mode: str = "hybrid", rrf_k: int = 60) -> List[Dict[str, Any]]:
        """
        检索文档片段：vector（语义）、bm25（词法）或 hybrid（两路排名用 RRF 融合）。

        department 给定时只返回该部门与公共（"*"）文档。
        """
        if type(top_k) is not int or not 1 <= top_k <= 20:
            raise ValueError("top_k 必须是 1–20 的整数")
        if mode not in {"hybrid", "vector", "bm25"}:
            raise ValueError("mode 必须是 hybrid、vector 或 bm25")
        version = self._collection.version()
        count = version[0]
        if not count:
            return []
        self._ensure_lexical_index(version)
        with self._index_lock:
            index, doc_ids = self._lexical_index, self._doc_ids
        mask = self._department_mask(department)

        merged: Dict[str, Dict[str, Any]] = {}

        def add(doc_id, rank, document, metadata, vector_score, bm25_score):
            entry = merged.setdefault(doc_id, {"doc": document, "meta": metadata, "vector_score": vector_score, "bm25_score": bm25_score, "rrf": 0.})
            entry["rrf"] += 1 / (rrf_k + rank)

        # 部门隔离在 SQL 里做（召回前过滤，其他部门的文档不会占满候选窗口），下面合并时再按 allowed 校验一次（纵深防御）。
        results = {}
        if mode != "bm25":
            where = {"department": {"$in": ["*", department]}} if department else None
            results = self._collection.query(query_texts=[query], n_results=min(top_k * 4, count), where=where)
        scores = self._bm25_array(query, mask) if mode != "vector" else np.zeros(len(doc_ids))
        positive = np.flatnonzero(scores > 0)
        order = positive[np.argsort(-scores[positive], kind="stable")]
        lexical_rank = np.zeros(len(doc_ids), dtype=np.int64)
        lexical_rank[order] = np.arange(1, len(order) + 1)
        vector_hits = zip(results.get("ids", [[]])[0], results.get("documents", [[]])[0], results.get("metadatas", [[]])[0], results.get("distances", [[]])[0])
        vector_positions = []
        for rank, (doc_id, document, metadata, distance) in enumerate(vector_hits, 1):
            pos = index[doc_id]["pos"] if doc_id in index else None
            if pos is not None and mask[pos]:
                add(doc_id, rank, document, metadata or {}, max(0.0, 1.0 - float(distance)), float(scores[pos]))
                vector_positions.append(pos)
        # 只展开词法排名前 top_k*4 的片段：排名更靠后、又不在向量结果里的片段，RRF 分低于词法前 top_k 名，进不了结果。
        window = top_k * 4
        for rank, pos in enumerate(order[:window], 1):
            row = index[doc_ids[pos]]
            add(row["id"], rank, row["document"], row["metadata"], 0., float(scores[pos]))
        for pos in vector_positions:
            if lexical_rank[pos] > window:
                merged[doc_ids[pos]]["rrf"] += 1 / (rrf_k + lexical_rank[pos])

        score_key = {"hybrid": "rrf", "vector": "vector_score", "bm25": "bm25_score"}[mode]
        ranked = sorted((item for item in merged.items() if mode == "hybrid" or item[1][score_key] > 0), key=lambda item: item[1][score_key], reverse=True)
        return [self._to_result(i, entry, mode) for i, (_, entry) in enumerate(ranked[:top_k])]

    @staticmethod
    def _to_result(position: int, entry: Dict[str, Any], mode: str) -> Dict[str, Any]:
        meta = entry["meta"]
        chunk_id = meta.get("chunk_id", position)
        score = entry["rrf"] if mode == "hybrid" else entry["vector_score"] if mode == "vector" else entry["bm25_score"]
        return {
            "title": meta.get("title", ""), "content": entry["doc"], "score": round(score, 6),
            "vector_score": round(entry["vector_score"], 6), "bm25_score": round(entry["bm25_score"], 6),
            "chunk": meta.get("chunk_index", 0), "chunk_id": chunk_id, "document_id": meta.get("document_id", ""),
            "version": meta.get("version", ""), "source": meta.get("source", meta.get("title", "")), "department": meta.get("department", "*"),
            "updated_at": meta.get("updated_at", ""), "citation": f"{meta.get('title', '')} · {chunk_id}",
        }

    async def search_async(self, query: str, top_k: int = 5, **kwargs) -> List[Dict[str, Any]]:
        """异步检索；embedding 与数据库访问是同步的，因此放入线程池执行。"""
        return await asyncio.to_thread(self.search, query, top_k, **kwargs)

    async def doc_count_async(self) -> int:
        """异步获取文档片段数量。"""
        return await asyncio.to_thread(self._collection.count)

    # ── MCP 工具 handler ─────────────────────────────────────────────────────

    async def search_handler(self, params: Dict[str, Any], context: Any) -> List[Dict]:
        """
        作为 MCP 工具的 handler 注册。

        MCPToolManager.register(Tool(
            name="knowledge_search",
            handler=kb.search_handler,
            ...
        ))
        """
        query = params.get("query", "")
        top_k = params.get("top_k", 5)
        return await self.search_async(query, top_k=top_k, department=params.get("department"), mode=params.get("mode", "hybrid"))

    # ── 内部方法 ──────────────────────────────────────────────────────────────

    def _chunk_text(self, text: str, chunk_size: int = 500) -> List[str]:
        """按句子边界把长文本装成不超过 chunk_size 的片段；中文按 。！？；和换行，英文按 .!?; 后接空白。

        切分点是零宽的，片段保留原文字符（换行、空格），超长的单句按 chunk_size 硬切。
        """
        if len(text) <= chunk_size:
            return [text] if text.strip() else []
        sentences = [part[i:i + chunk_size] for part in re.split(r"(?<=[。！？；\n])|(?<=[.!?;])(?=\s)", text)
                     for i in range(0, len(part), chunk_size)]
        chunks, current = [], ""
        for sent in sentences:
            if current and len(current) + len(sent) > chunk_size:
                chunks.append(current.strip())
                current = ""
            current += sent
        if current.strip():
            chunks.append(current.strip())
        return [c for c in chunks if c]

    @staticmethod
    def _knowledge_root() -> pathlib.Path:
        return pathlib.Path(os.getenv("IT_COPILOT_KNOWLEDGE_DIR", str(pathlib.Path(__file__).resolve().parent.parent / "knowledge")))

    def _load_default_docs(self) -> None:
        """导入演示知识库：knowledge/ 下每个 Markdown 是一篇文档，首行 `# 标题`，其余为正文。"""
        root = self._knowledge_root()
        docs = []
        for path in sorted(root.rglob("*.md")):
            title, _, content = path.read_text(encoding="utf-8").partition("\n")
            if title.startswith("# ") and content.strip():
                docs.append({"title": title[2:].strip(), "content": content.strip(), "source": path.relative_to(root).as_posix()})
        # 按标题身份比对内容哈希，未变化的文档不重新生成 embedding；同名但非演示来源的文档不覆盖。
        stale = [doc for doc in docs if self._seed_state(doc) == "stale"]
        if stale:
            self.add_documents(stale)
            logger.info("已导入默认知识库: %s/%s 篇文档有新增或更新", len(stale), len(docs))

    # 旧版内置演示文档（source 为标题）的内容哈希：只有这些可以被 knowledge/ 下的同名文件替换。
    _LEGACY_DEMO_VERSIONS = frozenset({
        "beceea6bf36e03bf53f9bea23dd46f2070ef85669f5e33af53b509636abba919",  # VPN 连接排障 SOP
        "8509e2372913c5c58e682a734384cbc749689050f858b588d28c949a6a79b097",  # 密码重置与 MFA
        "62ab9686bba110061da7543921788d00e827da9172012a5d7ae5cdb06bd6d1d7",  # 权限申请流程
        "c89408d5f53446ed1883601fa1600862c3e3aedff96e433e38d8ed8243d021b6",  # Wi-Fi 与网络排障
        "89ba23c7d7534f79668439e4ca68a4b35a39d4b1fdf9480395aa0ecada3c0bc3",  # 软件安装与许可证
        "133ccbb09436d7fcb434a4ff45552f21128ccd289aae83cb9c8b99c9ce405aa9",  # 工单优先级与 SLA
    })

    def _seed_state(self, doc: Dict[str, str]) -> str:
        """current：已是最新；stale：缺失或是演示种子的旧版本；foreign：同名文档来自管理员等其他来源，保留不动。"""
        document_id = hashlib.sha256(json.dumps(["title", doc["title"]], ensure_ascii=True).encode()).hexdigest()
        version = hashlib.sha256(doc["content"].encode()).hexdigest()
        metadatas = [meta or {} for meta in self._collection.get(where={"document_id": document_id})["metadatas"]]
        if not metadatas:
            return "stale"
        if all(meta.get("version") == version and meta.get("source") == doc["source"] for meta in metadatas):
            return "current"
        if all(meta.get("source") == doc["source"] or meta.get("version") in self._LEGACY_DEMO_VERSIONS for meta in metadatas):
            return "stale"
        logger.info("跳过演示文档《%s》：知识库中已有非演示来源的同名文档", doc["title"])
        return "foreign"
