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
from pydantic_ai.exceptions import ModelContextWindowExceededError, UnexpectedModelBehavior
from redis.exceptions import WatchError

from servicedesk_agent.infra.concurrency import KeyedLocks, redis_lease
from servicedesk_agent.agent.memory.context_budget import message_text, serialized, text_tokens
from servicedesk_agent.agent.memory.model_history import encode_turn
from servicedesk_agent.agent.memory.context import MemoryContext, Message, MsgRole, messages_from_raw, safe_text, trace_snapshot
from servicedesk_agent.agent.memory.recall import rank_memory_rows
from servicedesk_agent.agent.memory.states import COMPRESSION
from servicedesk_agent.agent.llm import complete_text
from servicedesk_agent.agent.model_router import MODEL_ERRORS, ModelRouter
from servicedesk_agent.infra.vector_store import VectorStore
from servicedesk_agent.agent.masking.redaction import redact, redact_text

logger = logging.getLogger(__name__)


class MemoryManager:
    WORKING_MAX_BYTES = 4 * 1024 * 1024
    # Explicit instance overrides support deterministic maintenance tests.
    COMPRESS_MIN_TOKENS = 24000
    COMPRESS_AT = 64
    HISTORY_TOP_K = 5     # 情景记忆检索返回条数
    MEMORY_MAX_TOKENS = 1200
    SUMMARY_MAX_CHARS = 800

    def __init__(
        self,
        redis_url:    str = "redis://localhost:6379/0",
        database_url: str = "sqlite:///data/servicedesk_agent.sqlite3",
        api_key:      str = "",
        base_url:     Optional[str] = None,
        model:        str = "",
        router=None,
    ):
        self._owns_router = router is None
        self.router = ModelRouter(api_key=api_key, base_url=base_url, model=model) if router is None else router

        self._redis = redis.from_url(redis_url, decode_responses=True)
        self._request_locks = KeyedLocks()
        self.context_mode = os.getenv("SERVICEDESK_AGENT_MEMORY_MODE", "full")
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
            async with redis_lease(self._redis, f"servicedesk_agent:lock:conversation:{digest}"):
                yield

    def receipt_key(self, user_id, conv_id, request_id):
        return "servicedesk_agent:turn_receipt:" + self._conversation_id(user_id, conv_id) + ":" + hashlib.sha256(request_id.encode()).hexdigest()

    async def get_turn_receipt(self, user_id, conv_id, request_id):
        raw = await self._redis.get(self.receipt_key(user_id, conv_id, request_id))
        return json.loads(raw) if raw else None

    async def commit_turn(self, user_id, conv_id, request_id, message, response, trace=None, *, state=None, model_prompt=None, new_messages=None, selection=None):
        """原子保存一整轮及响应回执；由 conversation 锁保护。"""
        def raw(role, content, **extra):
            return json.dumps({"role": role, "content": redact_text(safe_text(content)),
                "ts": datetime.now().isoformat(), "metadata": {"request_id": request_id, **extra}}, ensure_ascii=False)
        rows = []
        if response.get("status") != "failed":
            evidence = {"tools": list(response.get("tools_used") or []), "approval_id": response.get("approval_id"),
                        "awaiting_confirmation": bool(response.get("awaiting_confirmation"))}
            trajectory = encode_turn(new_messages, message, model_prompt)
            if trajectory:
                evidence["model_messages"] = trajectory
            prompt_meta = {"model_prompt": redact_text(safe_text(model_prompt))} if model_prompt is not None else {}
            rows = [raw("user", message, **prompt_meta), raw("assistant", response["response"], **evidence)]
        while True:
            try:
                async with self._redis.pipeline(transaction=True) as pipe:
                    if rows:
                        await self._append_working_turn(pipe, user_id, conv_id, rows, state)
                    pipe.set(self.receipt_key(user_id, conv_id, request_id),
                             json.dumps({"selection": selection, "message_hash": hashlib.sha256(message.encode()).hexdigest(), "response": redact(response)}), ex=86400)
                    if trace:
                        safe_trace = redact(trace)
                        # These verified identity fields authorize trace reads; text masking must
                        # not change their identity (e.g. an OIDC subject that resembles a phone).
                        scope = trace.get("observability", {})
                        for field in ("owner", "tenant"):
                            if field in scope:
                                safe_trace["observability"][field] = scope[field]
                        encoded_trace = json.dumps(safe_trace)
                        pipe.set("servicedesk_agent:trace:" + response["request_id"], encoded_trace, ex=7 * 86400)
                        pipe.lpush("servicedesk_agent:trace:recent", response["request_id"])
                        pipe.ltrim("servicedesk_agent:trace:recent", 0, 199)
                    await pipe.execute()
                return
            except WatchError:
                continue

    async def _append_working_turn(self, pipe, user_id, conv_id, rows, state):
        key = self._wm_key(user_id, conv_id)
        size_key = "servicedesk_agent:bytes:" + self._conversation_id(user_id, conv_id)
        skey = self._summary_key(user_id, conv_id)
        state_key = self._state_key(user_id, conv_id)
        await pipe.watch(key, size_key, skey, state_key)
        previous_size = await pipe.get(size_key)
        snapshot = None
        if previous_size is None:
            snapshot = await pipe.lrange(key, 0, -1)
            previous_size = sum(len(row.encode()) for row in snapshot)
        size = int(previous_size) + sum(len(row.encode()) for row in rows)
        dropped = []
        if size > self.WORKING_MAX_BYTES:
            if snapshot is None:
                snapshot = await pipe.lrange(key, 0, -1)
            current = [*reversed(rows), *snapshot]
            while current and (size > self.WORKING_MAX_BYTES or json.loads(current[-1])["role"] != "user"):
                row = current.pop()
                dropped.append(row)
                size -= len(row.encode())
            keep = len(current)
            archive_id = hashlib.sha256(serialized(dropped).encode()).hexdigest()
            archive_key = self._archive_key(user_id, conv_id)
            old = messages_from_raw(dropped[::-1])
            visible = MemoryContext(old, [], "").history_turns(limit=None)
            summary = ((await pipe.get(skey) or "") + "\n" + "\n".join(m["content"] for m in visible))[-self.SUMMARY_MAX_CHARS:]
            archive = dict(user_id=user_id, conv_id=conv_id, text="\n".join(dropped), summary=summary)
            state = dict(state if state is not None else json.loads(await pipe.get(state_key) or "{}"))
            for name in ("knowledge_fingerprints", "last_context_tokens", "last_context_prefix", "last_context_model"):
                state.pop(name, None)
        pipe.multi()
        pipe.lpush(key, *rows)
        if dropped:
            pipe.hset(archive_key, archive_id, serialized(archive))
            pipe.sadd(self.ARCHIVE_INDEX, archive_key)
            pipe.set(skey, summary, ex=86400)
            if keep:
                pipe.ltrim(key, 0, keep - 1)
            else:
                pipe.delete(key)
        pipe.set(size_key, size, ex=86400)
        pipe.expire(skey, 86400)
        pipe.expire(key, 86400)
        if state is not None:
            pipe.set(state_key, json.dumps(redact(state)), ex=86400)

    async def get_trace(self, request_id):
        raw = await self._redis.get("servicedesk_agent:trace:" + request_id)
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
        keys = await self._redis.lrange("servicedesk_agent:trace:recent", 0, max(1, min(limit, 200))-1)
        if not keys:
            return []
        rows = await self._redis.mget(["servicedesk_agent:trace:" + key for key in keys])
        return [json.loads(row) for row in rows if row]

    async def maintain(self, user_id, conv_id, *, summary_prefix=None):
        await self._flush_archives(user_id, conv_id)
        await asyncio.wait_for(self._compress(user_id, conv_id, summary_prefix=summary_prefix), 30)

    async def get_context(self, user_id: str, conv_id: str, query: str = "", intent_group: Optional[str] = None, entities: Optional[Dict[str, Any]] = None, max_history_tokens: Optional[int] = None, *, summary_prefix=None) -> MemoryContext:
        user_id = safe_text(user_id)
        conv_id = safe_text(conv_id)
        query = safe_text(query)

        recent = await self._get_working_memory(user_id, conv_id)
        if self._needs_compression(recent):
            await self.maintain(user_id, conv_id, summary_prefix=summary_prefix)
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
        if not recent:
            # The Redis window expired or compression was deferred past its cap.
            conversation_state.pop("knowledge_fingerprints", None)

        return MemoryContext(
            recent_messages=recent,
            relevant_history=history,
            summary=summary,
            state=conversation_state if self.context_mode != "working" else {
                key: conversation_state[key] for key in ("agent_spec", "agent_version", "knowledge_fingerprints", "last_context_tokens", "last_context_model", "last_context_prefix") if key in conversation_state},
        )

    async def get_state(self, user_id, conv_id):
        raw = await self._redis.get(self._state_key(user_id, conv_id))
        return json.loads(raw) if raw is not None else {}

    def _needs_compression(self, messages):
        if not messages:
            return False
        return ("COMPRESS_AT" in vars(self) and len(messages) >= self.COMPRESS_AT or
                "COMPRESS_MIN_TOKENS" in vars(self) and sum(text_tokens(message_text(m))
                    for m in MemoryContext(messages, [], "").model_history()) >= self.COMPRESS_MIN_TOKENS)

    async def compact_for_model(self, owner, conv_id, active, keep_tokens, summary_prefix):
        changed = await self._compress(owner, conv_id, active=active, keep_tokens=keep_tokens,
                                       summary_prefix=summary_prefix)
        if changed not in {"archived", "archive_pending"}:
            return None
        messages = await self._get_working_memory(owner, conv_id)
        summary = await self._redis.get(self._summary_key(owner, conv_id)) or ""
        return MemoryContext(messages, [], summary).model_history()

    async def _compress(self, user_id, conv_id, *, summary_prefix=None, active=None, keep_tokens=None):
        flow = COMPRESSION.start()
        key = self._wm_key(user_id, conv_id)
        state_key = self._state_key(user_id, conv_id)
        skey = self._summary_key(user_id, conv_id)
        # Read the entire snapshot so that every trimmed turn is represented in the handoff.
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.lrange(key, 0, -1)
            pipe.get(skey)
            snapshot, old_summary = await pipe.execute()
        messages = messages_from_raw(snapshot)
        if not messages or active is None and not self._needs_compression(messages):
            return flow.send("skip")
        flow.send("summarize")
        old_summary = old_summary or ""
        keep = min(4, max(0, len(messages) - 2))
        while keep and messages[-keep].role != MsgRole.USER:
            keep -= 1
        to_compress = messages[:len(messages) - keep]
        if keep_tokens is not None:
            keep = 0
            end = len(messages)
            for start in reversed(range(1, end)):
                if messages[start].role != MsgRole.USER:
                    continue
                turn = MemoryContext(messages[start:end], [], "").model_history()
                cost = sum(text_tokens(message_text(m)) for m in turn)
                if cost > keep_tokens:
                    break
                keep_tokens -= cost
                keep = len(messages) - start
                end = start
            to_compress = messages[:len(messages) - keep]
        history = MemoryContext(to_compress, [], old_summary).model_history()
        text = redact_text("\n".join(message_text(m) for m in history))
        prompt = "总结交接资料，分列用户目标、关键实体和ID（工单号、设备号、错误码、系统和角色）、已生成草稿与审批状态、未决约束。仅返回800字以内的摘要。"
        prefix = summary_prefix() if summary_prefix else None
        overhead = text_tokens(prompt + serialized(prefix)) + 128
        size = sum(text_tokens(message_text(m)) for m in history)
        async def summarize(routed, prompt, history=None, prefix=None):
            resp = await complete_text(routed.model, prompt, history=history, prefix=prefix,
                                       model_settings=routed.settings, max_tokens=1024)
            if not resp.text.strip() or resp.truncated:
                raise UnexpectedModelBehavior("incomplete summary")
            return redact_text(resp.text.strip())

        errors = (*MODEL_ERRORS, ModelContextWindowExceededError, UnexpectedModelBehavior)
        summary = None
        if active and size + overhead + 1024 < self.router.config.models[active.name].context_window:
            try:
                summary = await summarize(active, prompt, history, prefix)
            except errors:
                pass
        if summary is None:
            try:
                routed = self.router.pick("summary", conv_id=conv_id)
                budget = self.router.compaction_budget(routed.name)[0]
                if size + overhead < budget:
                    summary = await self.router.run(routed, summarize(routed, prompt, history, prefix))
                else:
                    # Stream through bounded chunks, incorporating the preceding handoff into each reduction.
                    summary = ""
                    pos = 0
                    while pos < len(text):
                        instruction = prompt + "\n上一段交接：" + summary + "\n本段资料："
                        available = budget - text_tokens(instruction) - 128
                        if available <= 0:
                            return flow.send("defer")
                        chunk = text[pos:pos + available * 3 // 4]
                        if not chunk:
                            return flow.send("defer")
                        summary = await self.router.run(routed, summarize(routed, instruction + chunk))
                        pos += len(chunk)
            except errors:
                return flow.send("defer")
        new_summary = summary[:self.SUMMARY_MAX_CHARS]
        flow.send("validate")
        archive_id = hashlib.sha256(json.dumps([user_id, conv_id, text, summary]).encode()).hexdigest()
        archive_key = self._archive_key(user_id, conv_id)
        # WATCH/MULTI 同时保护列表和摘要；并发新增的消息保留在列表头部。
        for _ in range(3):
            async with self._redis.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(key, skey, state_key)
                    current = await pipe.lrange(key, 0, -1)
                    current_summary = await pipe.get(skey) or ""
                    if len(current) < len(snapshot) or current[-len(snapshot):] != snapshot or current_summary != old_summary:
                        return flow.send("defer")  # snapshot replaced by another compressor
                    keep_count = len(current) - len(snapshot) + keep
                    state = json.loads(await pipe.get(state_key) or "{}")
                    pipe.multi()
                    if any(k in state for k in ("knowledge_fingerprints", "last_context_tokens")):
                        for k in ("knowledge_fingerprints", "last_context_tokens", "last_context_prefix", "last_context_model"):
                            state.pop(k, None)
                        pipe.set(state_key, json.dumps(state), ex=86400)
                    pipe.set(skey, new_summary, ex=86400)
                    pipe.hset(archive_key, archive_id, json.dumps({"text": text, "summary": summary, "user_id": user_id, "conv_id": conv_id}))
                    pipe.sadd(self.ARCHIVE_INDEX, archive_key)
                    if keep_count:
                        pipe.ltrim(key, 0, keep_count - 1)
                    else:
                        pipe.delete(key)
                    pipe.delete("servicedesk_agent:bytes:" + self._conversation_id(user_id, conv_id))
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
        raws = await self._redis.lrange(self._wm_key(user_id, conv_id), 0, -1)
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
        return "servicedesk_agent:memory:archives:" + self._conversation_id(user_id, conv_id)

    ARCHIVE_INDEX = "servicedesk_agent:memory:archives:pending"  # set of archive hashes that still hold unflushed items

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
        async with redis_lease(self._redis, f"servicedesk_agent:lock:episodic:{digest}"):
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
        from servicedesk_agent.infra.db import admin_audit, vector_documents

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
        if self._owns_router:
            await self.router.aclose()

    @classmethod
    def _conversation_id(cls, user_id: str, conv_id: str) -> str:
        return hashlib.sha256(json.dumps([safe_text(user_id), safe_text(conv_id)], ensure_ascii=True).encode()).hexdigest()

    @classmethod
    def _wm_key(cls, user_id: str, conv_id: str) -> str:
        return f"servicedesk_agent:wm_v2:{cls._conversation_id(user_id, conv_id)}"

    @classmethod
    def _summary_key(cls, user_id: str, conv_id: str) -> str:
        return f"servicedesk_agent:summary_v2:{cls._conversation_id(user_id, conv_id)}"

    @classmethod
    def _state_key(cls, user_id: str, conv_id: str) -> str:
        return f"servicedesk_agent:state_v1:{cls._conversation_id(user_id, conv_id)}"
