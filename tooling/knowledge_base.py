"""
RAG 知识库 —— 基于 ChromaDB 的真实检索实现。

功能：
  1. 文档导入：将文本切片后存入 ChromaDB（自动生成 Embedding）
  2. 语义检索：根据 query 从知识库中检索最相关的文档片段
  3. 与 MCP 工具框架集成：作为 knowledge_search 工具的真实 handler

ChromaDB 在这里的角色：
  - memory/ 中用于存储对话记忆（情景记忆 + 用户画像）
  - 这里用于存储知识库文档（RAG 检索）
  两者是不同的 collection，互不干扰。
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import math
from datetime import datetime, timezone
import threading
from typing import Any, Dict, List, Optional

import chromadb

logger = logging.getLogger(__name__)


class KnowledgeBase:
    """
    基于 ChromaDB 的 RAG 知识库。

    ChromaDB 内置了 Embedding 模型（all-MiniLM-L6-v2），
    调用 add() 时自动生成向量，query() 时自动做语义匹配。
    不需要额外调用 Anthropic Embeddings API。
    """

    COLLECTION_NAME = "knowledge_base"

    def __init__(
        self,
        chroma_host: str = "localhost",
        chroma_port: int = 8000,
        chroma_path: str = "./data/chroma",
    ):
        # 优先连接独立 ChromaDB 服务；默认 embedding_function 在客户端生成向量。
        self._write_lock = threading.RLock()
        self._index_lock = threading.RLock()
        self._lexical_index = []
        self._use_server = False
        try:
            # HttpClient 默认也会初始化 ChromaDB telemetry；显式关闭避免 posthog 兼容性错误日志。
            self._client = chromadb.HttpClient(
                host=chroma_host,
                port=chroma_port,
                settings=chromadb.Settings(anonymized_telemetry=False),
            )
            self._client.heartbeat()
            self._use_server = True
            logger.info(f"知识库 ChromaDB 已连接: {chroma_host}:{chroma_port}")
        except Exception as ex:
            if os.getenv("IT_COPILOT_ENV", "development").lower() == "production":
                raise RuntimeError("生产环境知识库 ChromaDB 不可用，禁止切换到本地库") from ex
            logger.info(f"知识库 ChromaDB 服务不可用，使用本地模式: {chroma_path}")
            self._client = chromadb.PersistentClient(
                path=chroma_path,
                settings=chromadb.Settings(anonymized_telemetry=False),
            )

        # 不传 embedding_function 时使用 ChromaDB 客户端默认模型；
        # Docker 已预下载模型，本地运行首次使用可能触发下载。
        self._collection = self._client.get_or_create_collection(
            name=self.COLLECTION_NAME,
            metadata={"description": "IT-Copilot 企业 IT 服务台知识库"},
        )

        # 如果知识库为空，导入默认文档
        seed_demo = os.getenv("IT_COPILOT_SEED_DEMO_DOCS", "true").lower() in {"1", "true", "yes"}
        if os.getenv("IT_COPILOT_ENV", "development").lower() != "production" and seed_demo and self._collection.count() == 0:
            self._load_default_docs()
        self._rebuild_lexical_index()

    # ── 文档管理 ──────────────────────────────────────────────────────────────

    def add_documents(self, documents: List[Dict[str, str]]) -> int:
        """
        批量导入文档到知识库。

        documents 格式: [{"title": "...", "content": "..."}, ...]
        长文档会自动切片（每片 500 字）。
        """
        # 相同文档身份在同一批次只采用最后一个版本，避免重复 ID。
        prepared = {}
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
            if not isinstance(identity, str):
                raise ValueError("document_id 必须是字符串")
            prepared[identity] = (title, content, bool(doc.get("document_id")))

        count = 0
        with self._write_lock:
            for identity, (title, content, explicit) in prepared.items():
                document_id = hashlib.sha256(identity.encode()).hexdigest()
                chunks = self._chunk_text(content, chunk_size=500)
                version = hashlib.sha256(content.encode()).hexdigest()
                ids = [f"{document_id}:{i}" for i in range(len(chunks))]
                existing = self._collection.get(where={"document_id": document_id}, include=["metadatas"])
                old_ids = set(existing["ids"])
                # 仅迁移归属可验证的旧身份；旧显式 ID 与 title: 前缀冲突时保留旧记录供核验。
                kind, value = json.loads(identity)
                legacy_identity = value if explicit else f"title:{title}"
                if not explicit or not value.startswith("title:"):
                    legacy_id = hashlib.sha256(legacy_identity.encode()).hexdigest()
                    previous = self._collection.get(where={"document_id": legacy_id}, include=["metadatas"])
                    old_ids.update(i for i, meta in zip(previous["ids"], previous["metadatas"])
                                   if (meta or {}).get("title") == title)
                if not explicit:
                    # 兼容旧版只有 title/chunk_index 元数据的片段。
                    legacy = self._collection.get(where={"title": title}, include=["metadatas"])
                    old_ids.update(i for i, meta in zip(legacy["ids"], legacy["metadatas"])
                                   if not (meta or {}).get("document_id"))
                # Metadata is deliberately explicit so hybrid retrieval can enforce
                # tenant scope and produce traceable citations.
                department = str(next((d.get("department") for d in documents
                                       if d.get("title") == title and d.get("department")), "*") or "*")
                source = str(next((d.get("source") for d in documents
                                   if d.get("title") == title and d.get("source")), title) or title)
                updated_at = str(next((d.get("updated_at") for d in documents
                                       if d.get("title") == title and d.get("updated_at")),
                                      datetime.now(timezone.utc).isoformat()))
                self._collection.upsert(ids=ids, documents=chunks, metadatas=[
                    {"title": title, "document_id": document_id, "version": version,
                     "chunk_index": i, "total_chunks": len(chunks), "source": source,
                     "department": department, "updated_at": updated_at,
                     "chunk_id": f"{document_id}:{i}"} for i in range(len(chunks))
                ])
                stale = old_ids - set(ids)
                if stale:
                    self._collection.delete(ids=list(stale))
                count += len(chunks)
            self._rebuild_lexical_index()
        logger.info("知识库写入 %s 个文档片段", count)
        return count

    async def add_documents_async(self, documents: List[Dict[str, str]]) -> int:
        """异步导入文档；ChromaDB 客户端为同步实现，因此放入线程池执行。"""
        return await asyncio.to_thread(self.add_documents, documents)

    @staticmethod
    def _tokens(text: str) -> List[str]:
        """Small dependency-free tokenizer for BM25; keeps CJK bigrams and words."""
        text = str(text or "").lower()
        words = re.findall(r"[a-z0-9_./-]+|[\u4e00-\u9fff]", text)
        cjk = [a + b for a, b in zip(words, words[1:]) if re.match(r"[\u4e00-\u9fff]", a) and re.match(r"[\u4e00-\u9fff]", b)]
        return words + cjk

    def _rebuild_lexical_index(self):
        with self._index_lock:
            data = self._collection.get(include=["documents", "metadatas"])
            self._lexical_index = []
            for doc_id, document, metadata in zip(data.get("ids", []), data.get("documents", []), data.get("metadatas", [])):
                tokens = self._tokens(document)
                self._lexical_index.append({"id": doc_id, "document": document, "metadata": metadata or {}, "tokens": tokens})

    def _bm25(self, query: str, candidates: Optional[set] = None) -> Dict[str, float]:
        with self._index_lock:
            rows = [r for r in self._lexical_index if candidates is None or r["id"] in candidates]
        q = self._tokens(query)
        if not q or not rows: return {}
        n, avgdl = len(rows), sum(len(r["tokens"]) for r in rows) / max(1, len(rows))
        df = {term: sum(term in r["tokens"] for r in rows) for term in set(q)}
        scores = {}
        for row in rows:
            tf = {term: row["tokens"].count(term) for term in set(q)}
            score = 0.0
            for term in set(q):
                if not tf[term]: continue
                idf = math.log(1 + (n - df[term] + .5) / (df[term] + .5))
                score += idf * (tf[term] * 2.0) / (tf[term] + 1.5 * (0.25 + 0.75 * len(row["tokens"]) / max(1, avgdl)))
            scores[row["id"]] = score
        return scores

    def search(self, query: str, top_k: int = 5, *, department: Optional[str] = None,
               mode: str = "hybrid", rrf_k: int = 60) -> List[Dict[str, Any]]:
        """
        语义检索：根据 query 返回最相关的文档片段。

        ChromaDB 内部自动将 query 转为向量，与存储的文档向量做余弦相似度匹配。
        """
        if type(top_k) is not int or not 1 <= top_k <= 20:
            raise ValueError("top_k 必须是 1–20 的整数")
        if mode not in {"hybrid", "vector", "bm25"}: raise ValueError("mode 必须是 hybrid、vector 或 bm25")
        count = self._collection.count()
        if not count:
            return []
        self._rebuild_lexical_index()
        allowed = {r["id"] for r in self._lexical_index if not department or r["metadata"].get("department", "*") in {"*", department}}
        vector_rows = []
        # Chroma's where syntax is intentionally avoided here: older and newer
        # Chroma servers differ in metadata filter behavior, so the tenant gate
        # is applied after the vector query as a defense-in-depth check.
        results = self._collection.query(query_texts=[query], n_results=min(max(top_k * 4, top_k), count), include=["documents", "metadatas", "distances"])
        for rank, (doc_id, doc, meta, dist) in enumerate(zip(results.get("ids", [[]])[0], results.get("documents", [[]])[0], results.get("metadatas", [[]])[0], results.get("distances", [[]])[0]), 1):
            if doc_id in allowed: vector_rows.append((doc_id, rank, doc, meta or {}, max(0.0, 1.0 - float(dist))))
        lexical_scores = self._bm25(query, allowed)
        lexical_rows = sorted(lexical_scores.items(), key=lambda item: item[1], reverse=True)
        rank_map = {doc_id: rank for rank, (doc_id, score) in enumerate(lexical_rows, 1) if score > 0}
        merged = {}
        for doc_id, rank, doc, meta, vector_score in vector_rows:
            merged.setdefault(doc_id, {"doc": doc, "meta": meta, "vector_score": vector_score, "bm25_score": lexical_scores.get(doc_id, 0.0)})["rrf"] = merged.get(doc_id, {}).get("rrf", 0) + (1 / (rrf_k + rank))
        for doc_id, rank in rank_map.items():
            row = next((r for r in self._lexical_index if r["id"] == doc_id), None)
            if row: merged.setdefault(doc_id, {"doc": row["document"], "meta": row["metadata"], "vector_score": 0., "bm25_score": lexical_scores[doc_id]})["rrf"] = merged.get(doc_id, {}).get("rrf", 0) + (1 / (rrf_k + rank))
        ranked = sorted(merged.items(), key=lambda item: item[1].get("rrf", 0), reverse=True)
        if mode == "vector": ranked = sorted(((i, v) for i, v in merged.items() if v.get("vector_score", 0) > 0), key=lambda item: item[1]["vector_score"], reverse=True)
        elif mode == "bm25": ranked = sorted(((i, v) for i, v in merged.items() if v.get("bm25_score", 0) > 0), key=lambda item: item[1]["bm25_score"], reverse=True)
        return [{"title": v["meta"].get("title", ""), "content": v["doc"], "score": round(v.get("rrf", 0), 6) if mode == "hybrid" else round(v.get("vector_score", v.get("bm25_score", 0)), 6), "vector_score": round(v.get("vector_score", 0), 6), "bm25_score": round(v.get("bm25_score", 0), 6), "chunk": v["meta"].get("chunk_index", 0), "chunk_id": v["meta"].get("chunk_id", i), "document_id": v["meta"].get("document_id", ""), "version": v["meta"].get("version", ""), "source": v["meta"].get("source", v["meta"].get("title", "")), "department": v["meta"].get("department", "*"), "updated_at": v["meta"].get("updated_at", ""), "citation": f"{v['meta'].get('title', '')} · {v['meta'].get('chunk_id', i)}"} for i, (doc_id, v) in enumerate(ranked[:top_k])]

    async def search_async(self, query: str, top_k: int = 5, **kwargs) -> List[Dict[str, Any]]:
        """异步检索；ChromaDB 客户端为同步实现，因此放入线程池执行。"""
        return await asyncio.to_thread(self.search, query, top_k, **kwargs)

    @property
    def doc_count(self) -> int:
        return self._collection.count()

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
        """将长文本按 chunk_size 切片，保留语义完整性（按句号/换行切分）。"""
        if len(text) <= chunk_size:
            return [text] if text.strip() else []

        chunks = []
        current = ""
        # 按句子切分
        sentences = text.replace("\n", "。").split("。")
        sentences = [sent[i:i + chunk_size] for sent in sentences
                     for i in range(0, len(sent), chunk_size)]
        for sent in sentences:
            sent = sent.strip()
            if not sent:
                continue
            if len(current) + len(sent) + 1 > chunk_size:
                if current:
                    chunks.append(current)
                current = sent
            else:
                current = f"{current}。{sent}" if current else sent

        if current:
            chunks.append(current)

        return chunks

    def _load_default_docs(self) -> None:
        """导入 IT 服务台的最小知识库。"""
        default_docs = [
            {"title":"VPN 连接排障 SOP", "content":"先确认企业账号未锁定，再检查系统时间、网络、DNS 和 VPN 客户端版本。记录错误码与发生时间。不得索取密码、验证码或私钥；仍失败时创建 P2 工单。"},
            {"title":"密码重置与 MFA", "content":"密码重置必须由员工本人通过企业身份平台发起并完成 MFA。服务台只创建待审批请求，不在聊天中接收或回显密码、OTP、恢复码。"},
            {"title":"权限申请流程", "content":"权限申请需要系统名称、业务理由、所需角色、有效期和直属负责人审批。遵循最小权限，离职或转岗触发权限复核。"},
            {"title":"Wi-Fi 与网络排障", "content":"收集地点、设备、SSID、错误提示和时间窗口，优先执行只读 DNS、路由和服务状态检查。不要修改网络策略；影响多人时升级网络团队。"},
            {"title":"软件安装与许可证", "content":"仅从企业软件目录安装，核对版本、兼容性和许可证归属。未知安装包、破解软件和绕过终端管控的请求必须拒绝并转人工。"},
            {"title":"工单优先级与 SLA", "content":"P1 为大范围业务中断，P2 为关键员工无法工作，P3 为单用户一般问题，P4 为咨询。创建工单后保留 ticket_id，所有变更需审计。"},
        ]
        self.add_documents(default_docs)
        logger.info(f"已导入默认知识库: {len(default_docs)} 篇文档")
