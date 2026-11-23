"""Redis working memory, summaries and episodic recall."""
import asyncio
import hashlib
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import redis.asyncio as redis
from pydantic_ai.exceptions import ModelConnectionError, ModelHTTPError, UnexpectedModelBehavior
from redis.exceptions import WatchError

from it_copilot.infra.concurrency import KeyedLocks, redis_lease
from it_copilot.agent.memory.context import MemoryContext, Message, MsgRole, messages_from_raw, safe_text, trace_snapshot
from it_copilot.agent.memory.recall import rank_memory_rows
from it_copilot.agent.memory.states import COMPRESSION
from it_copilot.agent.llm import close_model, complete_text
from it_copilot.agent.model_router import ModelRouter
from it_copilot.infra.vector_store import VectorStore
from it_copilot.agent.masking.redaction import redact, redact_text

logger = logging.getLogger(__name__)


class MemoryManager:
    WORKING_MAX   = 20    # 工作记忆最大条数，超过则触发压缩
    COMPRESS_AT   = 15    # 达到此条数时压缩，保留摘要 + 最近 5 条
    HISTORY_TOP_K = 5     # 情景记忆检索返回条数
    MEMORY_MAX_TOKENS = 1200
    SUMMARY_MAX_CHARS = 800

    def __init__(
        self,
        redis_url:    str = "redis://localhost:6379/0",
        database_url: str = "sqlite:///data/it_copilot.sqlite3",
        api_key:      str = "",
        base_url:     Optional[str] = None,
        model:        str = "",
        router=None,
    ):
        self.router = ModelRouter(api_key=api_key, base_url=base_url, model=model) if router is None else router

        self._redis = redis.from_url(redis_url, decode_responses=True)
        self._request_locks = KeyedLocks()
        self.context_mode = os.getenv("IT_COPILOT_MEMORY_MODE", "full")
        if self.context_mode not in {"working", "state", "full"}:
            raise ValueError("unknown memory mode")

        store = VectorStore(database_url)
        self.engine = store.engine
        self._episodic = store.collection("episodic")

    @property
    def _llm(self):
        return self.router.models[self.router.config.routes["summary"].candidates[0]]

    @_llm.setter
    def _llm(self, model):
        self.router.replace_models(model)

    @asynccontextmanager
    async def conversation(self, user_id: str, conv_id: str):
        # 哈希避免 user_id/conv_id 中的分隔符引起锁键碰撞。
        digest = self._conversation_id(user_id, conv_id)
        async with self._request_locks.hold(digest):
            async with redis_lease(self._redis, f"lock:conversation:{digest}"):
                yield

    def receipt_key(self, user_id, conv_id, request_id):
        return "turn_receipt:" + self._conversation_id(user_id, conv_id) + ":" + hashlib.sha256(request_id.encode()).hexdigest()

    async def get_turn_receipt(self, user_id, conv_id, request_id):
        raw = await self._redis.get(self.receipt_key(user_id, conv_id, request_id))
        return json.loads(raw) if raw else None

    async def commit_turn(self, user_id, conv_id, request_id, message, response, trace=None, *, state=None):
        """原子保存一整轮及响应回执；由 conversation 锁保护。"""
        def raw(role, content, **extra):
            return json.dumps({"role": role, "content": redact_text(safe_text(content)),
                "ts": datetime.now().isoformat(), "metadata": {"request_id": request_id, **extra}})
        key = self._wm_key(user_id, conv_id)
        async with self._redis.pipeline(transaction=True) as pipe:
            if response.get("status") != "failed":
                # 记下本轮的工具与审批：历史里只有回答文字时，后几轮模型找不到执行证据，会反过来否认自己做过的事。
                evidence = {"tools": list(response.get("tools_used") or []), "approval_id": response.get("approval_id"),
                            "awaiting_confirmation": bool(response.get("awaiting_confirmation"))}
                pipe.lpush(key, raw("user", message), raw("assistant", response["response"], **evidence))
                pipe.expire(key, 86400)
                if state is not None:
                    pipe.set(self._state_key(user_id, conv_id), json.dumps(redact(state)), ex=86400)
            pipe.set(self.receipt_key(user_id, conv_id, request_id),
                     json.dumps({"message_hash": hashlib.sha256(message.encode()).hexdigest(), "response": redact(response)}), ex=86400)
            if trace:
                safe_trace = redact(trace)
                # These verified identity fields authorize trace reads; text masking must
                # not change their identity (e.g. an OIDC subject that resembles a phone).
                scope = trace.get("observability", {})
                for field in ("owner", "tenant"):
                    if field in scope:
                        safe_trace["observability"][field] = scope[field]
                encoded_trace = json.dumps(safe_trace)
                pipe.set("trace:" + response["request_id"], encoded_trace, ex=7 * 86400)
                pipe.lpush("trace:recent", response["request_id"])
                pipe.ltrim("trace:recent", 0, 199)
            await pipe.execute()

    async def get_trace(self, request_id):
        raw = await self._redis.get("trace:" + request_id)
        return json.loads(raw) if raw else None

    async def turn_snapshot(self, owner, conv_id, request_id):
        trace = await self.get_trace(request_id)
        if trace is not None and "turn" in trace:
            return trace_snapshot(trace)
        messages = await self._get_working_memory(owner, conv_id)
        current = [m for m in messages if m.metadata.get("request_id") == request_id]
        return {"trace": trace, "turns": [m.content for m in current if m.role == MsgRole.USER],
                "response": "\n".join(m.content for m in current if m.role == MsgRole.ASSISTANT), "missing_slots": []}

    async def recent_traces(self, limit):
        keys = await self._redis.lrange("trace:recent", 0, max(1, min(limit, 200))-1)
        if not keys:
            return []
        rows = await self._redis.mget(["trace:" + key for key in keys])
        return [json.loads(row) for row in rows if row]

    async def maintain(self, user_id, conv_id):
        await self._flush_archives(user_id, conv_id)
        await asyncio.wait_for(self._compress(user_id, conv_id), 30)

    async def get_context(self, user_id: str, conv_id: str, query: str = "", intent_group: Optional[str] = None, entities: Optional[Dict[str, Any]] = None, max_history_tokens: Optional[int] = None) -> MemoryContext:
        user_id = safe_text(user_id)
        conv_id = safe_text(conv_id)
        query = safe_text(query)

        if await self._redis.llen(self._wm_key(user_id, conv_id)) >= self.COMPRESS_AT:
            await self.maintain(user_id, conv_id)
        recent = await self._get_working_memory(user_id, conv_id)

        history = await self._search_episodic(
            user_id,
            conv_id,
            query or (recent[-1].content if recent else ""),
            intent_group=intent_group,
            entities=entities,
            max_tokens=max_history_tokens or self.MEMORY_MAX_TOKENS,
        ) if self.context_mode == "full" else []


        summary = await self._redis.get(self._summary_key(user_id, conv_id)) or ""
        conversation_state = await self.get_state(user_id, conv_id)

        return MemoryContext(
            recent_messages=recent,
            relevant_history=history,
            summary=summary,
            state=conversation_state if self.context_mode != "working" else {
                key: conversation_state[key] for key in ("agent_spec", "agent_version") if key in conversation_state},
        )

    async def get_state(self, user_id, conv_id):
        raw = await self._redis.get(self._state_key(user_id, conv_id))
        return json.loads(raw) if raw is not None else {}

    async def _compress(self, user_id: str, conv_id: str) -> None:
        flow = COMPRESSION.start()
        key = self._wm_key(user_id, conv_id)
        skey = self._summary_key(user_id, conv_id)
        # 必须读取完整快照，不能只压缩最近 WORKING_MAX 条后删除整段列表。
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.lrange(key, 0, -1)
            pipe.get(skey)
            snapshot, old_summary = await pipe.execute()
        if len(snapshot) < self.COMPRESS_AT:
            return flow.send("skip")
        flow.send("summarize")
        old_summary = old_summary or ""
        messages = messages_from_raw(snapshot)
        to_compress = messages[:-5]
        text = redact_text(safe_text("\n".join(f"{m.role.value}: {m.content}" for m in to_compress)))
        prompt = safe_text(f"用 2-3 句话总结以下对话的关键信息。错误码、事件 ID、设备号、工单号、系统和角色名等标识原样保留：\n{text}")
        try:
            routed = self.router.pick("summary")
            resp = await self.router.run(routed, complete_text(routed.model, prompt, max_tokens=1024, temperature=0.0,
                                       request_timeout=routed.settings["timeout"]))
            summary = redact_text(safe_text(resp.text)).strip()
            if not summary or resp.truncated:
                logger.warning("摘要为空或被截断，保留原始会话消息")
                return flow.send("defer")
        except (TimeoutError, ModelConnectionError, ModelHTTPError, UnexpectedModelBehavior) as ex:
            logger.warning("摘要生成失败，保留原始会话消息: %s", type(ex).__name__)
            return flow.send("defer")

        new_summary = await self._merge_summary(old_summary, summary)
        flow.send("validate")
        archive_id = hashlib.sha256(json.dumps([user_id, conv_id, text, summary]).encode()).hexdigest()
        archive_key = self._archive_key(user_id, conv_id)
        # WATCH/MULTI 同时保护列表和摘要；并发新增的消息保留在列表头部。
        for _ in range(3):
            async with self._redis.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(key, skey)
                    current = await pipe.lrange(key, 0, -1)
                    current_summary = await pipe.get(skey) or ""
                    if len(current) < len(snapshot) or current[-len(snapshot):] != snapshot or current_summary != old_summary:
                        return flow.send("defer")  # snapshot replaced by another compressor
                    keep_count = len(current) - len(snapshot) + 5
                    pipe.multi()
                    pipe.set(skey, new_summary, ex=86400)
                    pipe.hset(archive_key, archive_id, json.dumps({"text": text, "summary": summary, "user_id": user_id, "conv_id": conv_id}))
                    pipe.sadd(self.ARCHIVE_INDEX, archive_key)
                    pipe.ltrim(key, 0, keep_count - 1)
                    pipe.expire(key, 86400)
                    await pipe.execute()
                    flow.send("commit")
                    logger.info("工作记忆压缩完成: %s/%s", user_id, conv_id)
                    archived = await self._flush_archives(user_id, conv_id)
                    return flow.send("archive" if archived else "defer")
                except WatchError:
                    flow.send("conflict")
                    continue
        logger.info("会话持续并发写入，本次压缩延期")
        return flow.send("defer")

    async def _get_working_memory(self, user_id: str, conv_id: str) -> List[Message]:
        raws = await self._redis.lrange(self._wm_key(user_id, conv_id), 0, self.WORKING_MAX - 1)
        return messages_from_raw(raws)

    async def _search_episodic(self, user_id: str, conv_id: str, query: str, intent_group: Optional[str] = None, entities: Optional[Dict[str, Any]] = None, max_tokens: int = MEMORY_MAX_TOKENS) -> List[str]:
        """仅检索其他会话，当前会话已有消息与摘要。"""
        query_text = safe_text(query).strip()
        if not query_text:
            return []
        conditions = [{"user_id": safe_text(user_id)}, {"conv_id": {"$ne": safe_text(conv_id)}}]
        if intent_group:
            conditions.append({"intent_group": safe_text(intent_group)})
        try:
            if not await asyncio.to_thread(self._episodic.count):
                return []
            results = await asyncio.to_thread(self._episodic.query, query_texts=[query_text],
                                              n_results=self.HISTORY_TOP_K, where={"$and": conditions})
            docs = rank_memory_rows(results, max_tokens, entities)
            return list(dict.fromkeys(doc.strip() for doc in docs))[: self.HISTORY_TOP_K]
        except Exception as ex:
            logger.warning(f"情景记忆检索失败: {ex}")
            return []

    def _archive_key(self, user_id, conv_id):
        return "memory:archives:" + self._conversation_id(user_id, conv_id)

    ARCHIVE_INDEX = "memory:archives:pending"  # set of archive hashes that still hold unflushed items

    async def flush_pending_archives(self, batch=64):
        """Replay failed vector writes from the index committed with each archive."""
        for key in await self._redis.srandmember(self.ARCHIVE_INDEX, batch) or []:
            pending = await self._redis.hgetall(key)
            if not pending:
                await self._unindex_archive(key)
                continue
            item = json.loads(next(iter(pending.values())))
            try:
                await self._flush_archives(item["user_id"], item["conv_id"])
            except Exception:
                logger.warning("pending memory archive replay failed", exc_info=True)

    async def _flush_archives(self, user_id, conv_id):
        """Redis outbox survives compression commits; deterministic vector IDs make replay safe."""
        key = self._archive_key(user_id, conv_id)
        pending = await self._redis.hgetall(key)
        for archive_id, raw in pending.items():
            item = json.loads(raw)
            if await self._store_episodic(user_id, conv_id, item["text"], item["summary"]) is False:
                return False
            await self._redis.hdel(key, archive_id)
        if pending:
            await self._unindex_archive(key)
        return True

    async def _unindex_archive(self, key):
        # WATCH: a compression committing a new item meanwhile keeps the key indexed.
        async with self._redis.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(key)
                if await pipe.hlen(key):
                    return
                pipe.multi()
                pipe.srem(self.ARCHIVE_INDEX, key)
                await pipe.execute()
            except WatchError:
                pass

    async def _store_episodic(self, user_id: str, conv_id: str, text: str, summary: str) -> bool:
        """旧压缩 outbox 的重放也必须经过脱敏。"""
        text = redact_text(safe_text(text))
        summary = redact_text(safe_text(summary))
        doc_id = hashlib.sha256(json.dumps([user_id, conv_id, text, summary]).encode()).hexdigest()
        await asyncio.to_thread(self._episodic.upsert, ids=[doc_id], documents=[summary],
            metadatas=[{"user_id": user_id, "conv_id": conv_id,
                        "ts": datetime.now().isoformat(), "full_text": text[:500]}])
        return True

    async def store_episodic_turn(self, user_id: str, conv_id: str, request_id: str, message: str, response: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """同一主体的近重复问答合并，锁覆盖最近邻查询和写入。"""
        safe_message = redact_text(safe_text(message))[:300]
        safe_response = redact_text(safe_text(response))[:300]
        doc = f"员工问题：{safe_message}\n回答要点：{safe_response}"
        doc_id = hashlib.sha256(f"turn:{request_id}".encode()).hexdigest()
        now = datetime.now(timezone.utc).isoformat()
        meta = {"user_id": user_id, "conv_id": conv_id, "request_id": request_id,
                "source_request_id": request_id, "ts": now, "last_seen": now,
                "seen": 1, "memory_type": "episodic_turn", "importance": 0.4,
                "approval_id": None, "ticket_id": None}
        if metadata is not None:
            for key in ("intent", "intent_group", "approval_id", "ticket_id", "importance", "source_request_id"):
                if key in metadata:
                    meta[key] = metadata[key]
            if "entities" in metadata:
                meta["entity_keys"] = ",".join(sorted(metadata["entities"]))[:256]
        meta = redact(meta)
        digest = hashlib.sha256(user_id.encode()).hexdigest()
        async with redis_lease(self._redis, f"lock:episodic:{digest}"):
            hits = await asyncio.to_thread(self._episodic.query, query_texts=[doc], n_results=1,
                                          where={"user_id": user_id, "memory_type": "episodic_turn"})
            if hits["ids"][0] and hits["distances"][0][0] < 0.08:
                doc_id = hits["ids"][0][0]
                previous = hits["metadatas"][0][0]
                if previous["source_request_id"] == request_id:
                    return
                meta["seen"] = previous["seen"] + 1
            await asyncio.to_thread(self._episodic.upsert, ids=[doc_id], documents=[doc], metadatas=[meta])

    async def personal_memories(self, user_id: str) -> dict:
        rows = await asyncio.to_thread(self._episodic.get, where={"user_id": user_id})
        return {"episodic": [
            {"id": doc_id, "summary": document, "at": meta["ts"]}
            for doc_id, document, meta in zip(rows["ids"], rows["documents"], rows["metadatas"])]}

    def delete_personal_memories(self, principal, episodic_id: Optional[str] = None) -> int:
        from sqlalchemy import delete, insert, select
        from it_copilot.infra.db import admin_audit, vector_documents

        own = vector_documents.c.meta["user_id"].as_string() == principal.scope_key
        condition = own & vector_documents.c.collection.in_(("episodic", "user_profile"))
        if episodic_id is not None:
            condition = own & (vector_documents.c.collection == "episodic") & (vector_documents.c.id == episodic_id)
        with self.engine.begin() as db:
            ids = db.execute(select(vector_documents.c.id).where(condition)).all()
            if episodic_id is not None and not ids:
                return 0
            count = db.execute(delete(vector_documents).where(condition)).rowcount
            db.execute(insert(admin_audit).values(
                tenant=principal.tenant, actor=principal.subject, event="memory_deleted",
                subject=episodic_id, detail={"count": count, "scope": "episodic" if episodic_id else "all"},
                at=datetime.now(timezone.utc).timestamp(), outcome="succeeded", ref=None))
        return count

    async def ping(self) -> None:
        """确认 Redis 可达；锁和幂等依赖它，不可达时调用方应拒绝启动。"""
        await self._redis.ping()

    async def close(self) -> None:
        """关闭异步 Redis 连接。"""
        await self._redis.aclose()
        await close_model(self._llm)

    @classmethod
    def _conversation_id(cls, user_id: str, conv_id: str) -> str:
        return hashlib.sha256(json.dumps([safe_text(user_id), safe_text(conv_id)], ensure_ascii=True).encode()).hexdigest()

    @classmethod
    def _wm_key(cls, user_id: str, conv_id: str) -> str:
        return f"wm_v2:{cls._conversation_id(user_id, conv_id)}"

    @classmethod
    def _summary_key(cls, user_id: str, conv_id: str) -> str:
        return f"summary_v2:{cls._conversation_id(user_id, conv_id)}"

    @classmethod
    def _state_key(cls, user_id: str, conv_id: str) -> str:
        return f"state_v1:{cls._conversation_id(user_id, conv_id)}"

    async def _merge_summary(self, old_summary: str, new_summary: str) -> str:
        old_summary = redact_text(safe_text(old_summary)).strip()
        new_summary = redact_text(safe_text(new_summary)).strip()
        if not old_summary:
            return new_summary[: self.SUMMARY_MAX_CHARS]
        if not new_summary:
            return old_summary[: self.SUMMARY_MAX_CHARS]

        prompt = (f"请将两段对话摘要合并为不超过 {self.SUMMARY_MAX_CHARS} 个中文字符的正文。"
                  "保留用户偏好、关键实体、待办、约束和未解决问题。"
                  f"\n旧摘要：\n{old_summary}\n新增摘要：\n{new_summary}")
        try:
            routed = self.router.pick("summary")
            resp = await self.router.run(routed, complete_text(routed.model, prompt, max_tokens=256, temperature=0.0,
                                       request_timeout=routed.settings["timeout"]))
            merged = redact_text(safe_text(resp.text)).strip()
            if merged:
                return merged[: self.SUMMARY_MAX_CHARS]
        except (TimeoutError, ModelConnectionError, ModelHTTPError, UnexpectedModelBehavior) as ex:
            logger.warning(f"合并摘要失败，回退为截断拼接: {ex}")

        merged = safe_text(f"{old_summary}\n{new_summary}").strip()
        return merged[-self.SUMMARY_MAX_CHARS :]
