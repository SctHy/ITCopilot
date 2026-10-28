"""
亮点：多轮对话记忆管理

三级记忆架构，模拟人类记忆机制：
  1. 工作记忆（Redis）—— 当前会话的最近 N 条消息，毫秒级读写
  2. 情景记忆（ChromaDB）—— 跨会话的历史对话，按语义相似度检索
  3. 用户画像（ChromaDB）—— 从对话中提炼的长期偏好和实体

关键设计：
  - 上下文构建时三级记忆融合，按重要性 + 时效性排序
  - 工作记忆超过阈值时自动压缩（LLM 摘要），防止 context 爆炸
  - Embedding 由 ChromaDB 默认模型生成，LLM 用于摘要和画像提炼
"""
import hashlib
import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

import chromadb
import redis.asyncio as redis
from redis.exceptions import WatchError
from pydantic import BaseModel, ConfigDict
from pydantic_ai import Agent
from pydantic_ai.output import ToolOutput
from pydantic_ai.settings import ModelSettings

from core.llm import build_model, close_model, complete_text
from core.concurrency import KeyedLocks, redis_lease

logger = logging.getLogger(__name__)


class ProfileExtraction(BaseModel):
    """画像提炼的结构化输出；写入前仍经 _normalize_profile 脱敏和限长。"""
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)
    preferences:      List[str] = []
    department:       str = ""
    systems:          List[str] = []
    devices:          List[str] = []
    recurring_issues: List[str] = []
    entities:         Dict[str, List[str]] = {}


_PROFILE_AGENT = Agent(output_type=ToolOutput(ProfileExtraction, name="save_profile", description="保存提炼后的员工 IT 画像"), retries=0, name="profile_extractor")


class MsgRole(Enum):
    USER      = "user"
    ASSISTANT = "assistant"
    SYSTEM    = "system"


@dataclass
class Message:
    role:       MsgRole
    content:    str
    timestamp:  datetime = field(default_factory=datetime.now)
    metadata:   Dict[str, Any] = field(default_factory=dict)


@dataclass
class MemoryContext:
    """传给 Agent 的完整上下文。"""
    recent_messages:  List[Message]   # 工作记忆：最近对话
    relevant_history: List[str]       # 情景记忆：语义相关的历史片段
    user_profile:     Dict[str, Any]  # 用户画像：偏好、常用实体
    summary:          str             # 当前会话摘要（压缩后）
    state: Dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def _clean(text: str) -> str:
        """移除 Unicode 代理字符，防止编码错误。"""
        return text.encode("utf-8", errors="ignore").decode("utf-8")

    def to_prompt_text(self) -> str:
        """将记忆上下文格式化为 LLM 可用的文本。"""
        parts = []
        if self.summary:
            parts.append(f"[会话摘要]\n{self._clean(self.summary)}")
        if self.relevant_history:
            parts.append("[相关历史]\n" + "\n".join(f"- {self._clean(h)}" for h in self.relevant_history[:3]))
        if self.user_profile:
            parts.append(f"[用户画像]\n{json.dumps(self.user_profile, ensure_ascii=True)}")
        if self.recent_messages:
            parts.append("[最近对话]")
            for m in self.recent_messages[-8:]:
                parts.append(f"{m.role.value}: {self._clean(m.content)}")
        return "\n\n".join(parts)


class MemoryManager:
    """
    三级记忆管理器。

    工作记忆存 Redis（TTL 24h），情景记忆和用户画像存 ChromaDB（持久化）。
    """

    WORKING_MAX   = 20    # 工作记忆最大条数，超过则触发压缩
    COMPRESS_AT   = 15    # 达到此条数时压缩，保留摘要 + 最近 5 条
    HISTORY_TOP_K = 5     # 情景记忆检索返回条数
    MEMORY_MAX_TOKENS = 1200
    MEMORY_HALF_LIFE_DAYS = 30.0
    SUMMARY_MAX_CHARS = 800
    PROFILE_DOC_PREFIX = "user_profile:"

    def __init__(
        self,
        redis_url:    str = "redis://localhost:6379/0",
        chroma_host:  str = "localhost",
        chroma_port:  int = 8000,
        chroma_path:  str = "./data/chroma",
        api_key:      str = "",
        base_url:     Optional[str] = None,
        model:        str = "claude-3-5-sonnet-20241022",
    ):
        self._llm   = build_model(api_key, base_url, model)
        self._model = model

        self._redis = redis.from_url(redis_url, decode_responses=True)
        self._request_locks = KeyedLocks()
        self.context_mode = os.getenv("IT_COPILOT_MEMORY_MODE", "full")
        if self.context_mode not in {"working", "state", "full"}:
            raise ValueError("unknown memory mode")

        # ChromaDB：优先连接独立服务（docker compose 模式），连不上则降级为本地嵌入式
        try:
            # HttpClient 默认也会初始化 ChromaDB telemetry；显式关闭避免 posthog 兼容性错误日志。
            chroma = chromadb.HttpClient(
                host=chroma_host,
                port=chroma_port,
                settings=chromadb.Settings(anonymized_telemetry=False),
            )
            chroma.heartbeat()  # 测试连接
            logger.info(f"ChromaDB 已连接: {chroma_host}:{chroma_port}")
        except Exception as ex:
            if os.getenv("IT_COPILOT_ENV", "development").lower() == "production":
                raise RuntimeError("生产环境记忆 ChromaDB 不可用，禁止切换到本地库") from ex
            logger.info(f"ChromaDB 服务不可用，使用本地嵌入式模式: {chroma_path}")
            chroma = chromadb.PersistentClient(
                path=chroma_path,
                settings=chromadb.Settings(anonymized_telemetry=False),
            )

        # 情景记忆：存储历史对话片段
        self._episodic = chroma.get_or_create_collection("episodic")
        # 用户画像：存储提炼出的偏好和实体
        self._profile  = chroma.get_or_create_collection("user_profile")

    @asynccontextmanager
    async def conversation(self, user_id: str, conv_id: str):
        # 哈希避免 user_id/conv_id 中的分隔符引起锁键碰撞。
        digest = hashlib.sha256(json.dumps([self._safe_text(user_id), self._safe_text(conv_id)]).encode()).hexdigest()
        async with self._request_locks.hold(digest):
            async with redis_lease(self._redis, f"lock:conversation:{digest}"):
                yield

    # ── 写入 ──────────────────────────────────────────────────────────────────

    def receipt_key(self, user_id, conv_id, request_id):
        return "turn_receipt:" + self._conversation_id(user_id, conv_id) + ":" + hashlib.sha256(request_id.encode()).hexdigest()

    async def get_turn_receipt(self, user_id, conv_id, request_id):
        raw = await self._redis.get(self.receipt_key(user_id, conv_id, request_id))
        return json.loads(raw) if raw else None

    async def commit_turn(self, user_id, conv_id, request_id, message, response, trace=None, *, state=None):
        """原子保存一整轮及响应回执；由 conversation 锁保护。"""
        def raw(role, content):
            return json.dumps({"role": role, "content": self._safe_text(content),
                "ts": datetime.now().isoformat(), "metadata": {"request_id": request_id}})
        key = self._wm_key(user_id, conv_id)
        async with self._redis.pipeline(transaction=True) as pipe:
            if response.get("status") != "failed":
                pipe.lpush(key, raw("user", message), raw("assistant", response["response"]))
                pipe.expire(key, 86400)
                if state is not None:
                    pipe.set(self._state_key(user_id, conv_id), json.dumps(state), ex=86400)
            pipe.set(self.receipt_key(user_id, conv_id, request_id),
                     json.dumps({"message": message, "response": response}), ex=86400)
            if trace:
                from core.redaction import redact
                encoded_trace = json.dumps(redact(trace))
                pipe.set("trace:" + response["request_id"], encoded_trace, ex=7 * 86400)
                pipe.lpush("trace:recent", response["request_id"])
                pipe.ltrim("trace:recent", 0, 199)
            await pipe.execute()

    async def get_trace(self, request_id):
        raw = await self._redis.get("trace:" + request_id)
        return json.loads(raw) if raw else None

    async def recent_traces(self, limit):
        keys = await self._redis.lrange("trace:recent", 0, max(1, min(limit, 200))-1)
        if not keys:
            return []
        rows = await self._redis.mget(["trace:" + key for key in keys])
        return [json.loads(row) for row in rows if row]

    async def maintain(self, user_id, conv_id):
        # _compress 的快照与 WATCH 提交已保护一致性；生成摘要不占聊天锁。
        await asyncio.wait_for(self._compress(user_id, conv_id), 30)
        await self.update_profile(user_id, conv_id)

    async def update_profile(self, user_id: str, conv_id: str) -> None:
        digest = hashlib.sha256(self._safe_text(user_id).encode()).hexdigest()
        async with redis_lease(self._redis, f"lock:profile:{digest}"):
            await self._update_profile(user_id, conv_id)

    async def _update_profile(self, user_id: str, conv_id: str) -> None:
        """
        从当前工作记忆中提炼用户偏好，更新用户画像。
        用 LLM 提炼偏好，然后存入 ChromaDB（ChromaDB 内置 embedding，不依赖外部 API）。
        """
        user_id = self._safe_text(user_id)
        conv_id = self._safe_text(conv_id)
        messages = await self._get_working_memory(user_id, conv_id)
        if not messages:
            return

        current_profile = await self._get_profile(user_id)

        text = self._safe_text("\n".join(f"{m.role.value}: {m.content}" for m in messages[-10:]))
        profile_ctx = json.dumps(current_profile, ensure_ascii=False) if current_profile else "{}"
        prompt = f"""你是企业 IT 服务台的长期记忆提炼器。只保留非敏感、长期有用的信息，禁止保存密码、OTP、恢复码、私钥、完整 token 或秘密。
从以下对话和已有用户画像中提炼或更新员工的 IT 画像，通过 save_profile 返回。
对话:
{text}

已有画像:
{profile_ctx}

返回格式: {{"preferences": [], "department": "", "systems": [], "devices": [], "recurring_issues": [], "entities": {{"employee_id": [], "device_id": [], "ticket_id": [], "system": [], "role": [], "error_code": []}}}}"""
        prompt = self._safe_text(prompt)

        try:
            result = await asyncio.wait_for(_PROFILE_AGENT.run(
                prompt, model=self._llm, model_settings=ModelSettings(max_tokens=512, temperature=0.0),
            ), timeout=15)
            profile_data = self._merge_profile(current_profile, self._normalize_profile(result.output.model_dump()))

            doc_id = self._profile_doc_id(user_id)
            doc_text = self._safe_text(json.dumps(profile_data, ensure_ascii=False))

            # 直接传 documents，让 ChromaDB 内置模型生成 embedding（不依赖 Voyage API）
            await asyncio.to_thread(
                self._profile.upsert,
                ids=[doc_id],
                documents=[doc_text],
                metadatas=[{
                    "user_id": user_id,
                    "conv_id": conv_id,
                    "updated_at": datetime.now().isoformat(),
                }],
            )
            logger.info(f"用户画像已更新: {user_id}")
        except Exception as ex:
            logger.warning(f"更新用户画像失败: {ex}")

    @staticmethod
    def _normalize_profile(value: Any) -> Dict[str, Any]:
        """限制画像字段和长度，防止把敏感内容或任意模型输出写入长期记忆。"""
        if not isinstance(value, dict):
            return {}
        blocked = ("password", "passwd", "otp", "token", "secret", "private_key", "恢复码")
        def clean(item):
            text = str(item)[:120]
            return "[REDACTED]" if any(word in text.lower() for word in blocked) else text
        output = {"schema_version": 3, "stable_attributes": {}, "facts": []}
        for key in ("preferences", "systems", "devices", "recurring_issues"):
            values = value.get(key, [])
            output[key] = list(dict.fromkeys(clean(item) for item in values if isinstance(values, list)))[:20]
        output["department"] = clean(value.get("department", "")) if value.get("department") else ""
        output["stable_attributes"]["department"] = output["department"]
        output["stable_attributes"]["systems"] = list(output.get("systems", []))
        output["stable_attributes"]["devices"] = list(output.get("devices", []))
        entities = value.get("entities", {}) if isinstance(value.get("entities", {}), dict) else {}
        output["entities"] = {str(key): list(dict.fromkeys(clean(item) for item in values if isinstance(values, list)))[:8] for key, values in entities.items() if isinstance(values, list)}
        output["stable_attributes"]["entities"] = output["entities"]
        now = datetime.now().isoformat()
        for key, values in output["stable_attributes"].items():
            values = values if isinstance(values, list) else ([values] if values else [])
            for item in values:
                output["facts"].append({"key": key, "value": item, "status": "active", "updated_at": now, "importance": 0.7, "source": "profile_extraction"})
        return output

    @staticmethod
    def _merge_profile(previous: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
        """Merge facts instead of replacing the entire long-term profile."""
        if not previous:
            return incoming
        merged = dict(incoming)
        for key in ("preferences", "systems", "devices", "recurring_issues"):
            merged[key] = list(dict.fromkeys((previous.get(key) or []) + (incoming.get(key) or [])))[:20]
        merged["department"] = incoming.get("department") or previous.get("department", "")
        merged["entities"] = dict(previous.get("entities") or {})
        for key, values in (incoming.get("entities") or {}).items():
            merged["entities"][key] = list(dict.fromkeys((merged["entities"].get(key) or []) + values))[:8]
        merged["stable_attributes"] = {"department": merged["department"], "systems": merged["systems"], "devices": merged["devices"], "entities": merged["entities"]}
        old_facts = [dict(f) for f in (previous.get("facts") or []) if isinstance(f, dict)]
        new_facts = [f for f in (incoming.get("facts") or []) if isinstance(f, dict)]
        for old in old_facts:
            if any(old.get("key") == new.get("key") and old.get("value") != new.get("value") for new in new_facts):
                old["status"] = "superseded"
        merged["facts"] = old_facts + new_facts[-50:]
        merged["schema_version"] = 3
        return merged

    # ── 读取 ──────────────────────────────────────────────────────────────────

    async def get_context(self, user_id: str, conv_id: str, query: str = "", intent_group: Optional[str] = None, entities: Optional[Dict[str, Any]] = None, max_history_tokens: Optional[int] = None) -> MemoryContext:
        """
        构建完整的记忆上下文。

        query 用于从情景记忆中检索语义相关的历史片段。
        """
        # 1. 工作记忆（当前会话最近消息）
        user_id = self._safe_text(user_id)
        conv_id = self._safe_text(conv_id)
        query = self._safe_text(query)

        recent = await self._get_working_memory(user_id, conv_id)

        # 2. 情景记忆（跨会话语义检索）
        history = await self._search_episodic(
            user_id,
            conv_id,
            query or (recent[-1].content if recent else ""),
            intent_group=intent_group,
            entities=entities,
            max_tokens=max_history_tokens or self.MEMORY_MAX_TOKENS,
        ) if self.context_mode == "full" else []

        # 3. 用户画像
        profile = await self._get_profile(user_id) if self.context_mode == "full" else {}

        # 4. 会话摘要（如果已压缩过）
        summary = await self._redis.get(self._summary_key(user_id, conv_id)) or ""
        state_raw = await self._redis.get(self._state_key(user_id, conv_id))

        return MemoryContext(
            recent_messages=recent,
            relevant_history=history,
            user_profile=profile,
            summary=summary,
            state=json.loads(state_raw) if state_raw and self.context_mode != "working" else {},
        )

    # ── 压缩（防止 context 爆炸）─────────────────────────────────────────────

    async def _compress(self, user_id: str, conv_id: str) -> None:
        """
        工作记忆压缩：
          1. 用 LLM 对旧消息生成摘要
          2. 摘要存 Redis（覆盖旧摘要）
          3. 旧消息存入情景记忆（ChromaDB）供跨会话检索
          4. 工作记忆只保留最近 5 条
        """
        key = self._wm_key(user_id, conv_id)
        skey = self._summary_key(user_id, conv_id)
        # 必须读取完整快照，不能只压缩最近 WORKING_MAX 条后删除整段列表。
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.lrange(key, 0, -1)
            pipe.get(skey)
            snapshot, old_summary = await pipe.execute()
        if len(snapshot) < self.COMPRESS_AT:
            return
        old_summary = old_summary or ""
        messages = self._messages_from_raw(snapshot)
        to_compress = messages[:-5]
        text = self._safe_text("\n".join(f"{m.role.value}: {m.content}" for m in to_compress))
        prompt = self._safe_text(f"用 2-3 句话总结以下对话的关键信息：\n{text}")
        try:
            resp = await complete_text(self._llm, prompt, max_tokens=1024, temperature=0.0)
            summary = self._safe_text(resp.text).strip()
            if not summary or resp.truncated:
                logger.warning("摘要为空或被截断，保留原始会话消息")
                return
        except Exception as ex:
            logger.warning("摘要生成失败，保留原始会话消息: %s", type(ex).__name__)
            return

        new_summary = await self._merge_summary(old_summary, summary)
        # WATCH/MULTI 同时保护列表和摘要；并发新增的消息保留在列表头部。
        for _ in range(3):
            async with self._redis.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(key, skey)
                    current = await pipe.lrange(key, 0, -1)
                    current_summary = await pipe.get(skey) or ""
                    if len(current) < len(snapshot) or current[-len(snapshot):] != snapshot or current_summary != old_summary:
                        return  # 其他压缩任务已更新快照，不再覆盖它。
                    keep_count = len(current) - len(snapshot) + 5
                    pipe.multi()
                    pipe.set(skey, new_summary, ex=86400)
                    pipe.ltrim(key, 0, keep_count - 1)
                    pipe.expire(key, 86400)
                    await pipe.execute()
                    logger.info("工作记忆压缩完成: %s/%s", user_id, conv_id)
                    await self._store_episodic(user_id, conv_id, text, summary)
                    return
                except WatchError:
                    continue
        logger.info("会话持续并发写入，本次压缩延期")

    @staticmethod
    def _messages_from_raw(raws: List[str]) -> List[Message]:
        msgs = []
        for raw in reversed(raws):
            d = json.loads(raw)
            msgs.append(Message(role=MsgRole(d["role"]), content=d["content"],
                                timestamp=datetime.fromisoformat(d["ts"]), metadata=d.get("metadata", {})))
        return msgs

    async def _get_working_memory(self, user_id: str, conv_id: str) -> List[Message]:
        raws = await self._redis.lrange(self._wm_key(user_id, conv_id), 0, self.WORKING_MAX - 1)
        return self._messages_from_raw(raws)

    async def _search_episodic(self, user_id: str, conv_id: str, query: str, intent_group: Optional[str] = None, entities: Optional[Dict[str, Any]] = None, max_tokens: int = MEMORY_MAX_TOKENS) -> List[str]:
        """语义检索情景记忆。ChromaDB 内置 embedding，不依赖外部 API。"""
        query_text = self._safe_text(query).strip()
        if not query_text:
            return []
        try:
            where = {"$and": [{"user_id": self._safe_text(user_id)}, {"conv_id": self._safe_text(conv_id)}]}
            if intent_group:
                where["$and"].append({"intent_group": self._safe_text(intent_group)})
            results = await self._query_episodic(
                query_text,
                n_results=self.HISTORY_TOP_K,
                where=where,
            )
            docs = self._rank_memory_rows(results, max_tokens, entities)
            if len(docs) < self.HISTORY_TOP_K:
                fallback_where = {"user_id": self._safe_text(user_id)}
                if intent_group:
                    fallback_where = {"$and": [{"user_id": self._safe_text(user_id)}, {"intent_group": self._safe_text(intent_group)}]}
                fallback = await self._query_episodic(
                    query_text,
                    n_results=self.HISTORY_TOP_K,
                    where=fallback_where,
                )
                docs.extend(self._rank_memory_rows(fallback, max_tokens, entities))
            return self._budget_texts(self._dedupe_texts(docs), max_tokens)[: self.HISTORY_TOP_K]
        except Exception as ex:
            logger.warning(f"情景记忆检索失败: {ex}")
            return []

    async def _store_episodic(self, user_id: str, conv_id: str, text: str, summary: str) -> None:
        """将压缩后的对话片段存入情景记忆。ChromaDB 内置 embedding，不依赖外部 API。"""
        try:
            user_id = self._safe_text(user_id)
            conv_id = self._safe_text(conv_id)
            text = self._safe_text(text)
            summary = self._safe_text(summary)
            doc_id = hashlib.md5(f"{user_id}{conv_id}{time.time()}".encode()).hexdigest()
            # 直接传 documents，ChromaDB 内置模型自动生成 embedding
            await asyncio.to_thread(
                self._episodic.add,
                ids=[doc_id],
                documents=[summary],
                metadatas=[{"user_id": user_id, "conv_id": conv_id,
                            "ts": datetime.now().isoformat(), "full_text": self._safe_text(text[:500])}],
            )
        except Exception as ex:
            logger.warning(f"存储情景记忆失败: {ex}")

    async def store_episodic_turn(self, user_id: str, conv_id: str, request_id: str, message: str, response: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Phase 2：每轮将非敏感对话摘要写入 Chroma，支持跨会话语义召回。"""
        try:
            safe_message = self._safe_text(message)[:2000]
            safe_response = self._safe_text(response)[:3000]
            doc = f"员工问题：{safe_message}\n服务台回答：{safe_response}"
            doc_id = hashlib.sha256(f"turn:{request_id}".encode()).hexdigest()
            meta = {"user_id": self._safe_text(user_id), "conv_id": self._safe_text(conv_id), "request_id": self._safe_text(request_id), "ts": datetime.now().isoformat(), "memory_type": "episodic_turn", "importance": 0.5, "superseded": False}
            for key, value in (metadata or {}).items():
                if key in {"intent", "intent_group", "agent_type"}:
                    meta[key] = self._safe_text(value)[:64]
            if metadata and metadata.get("entities"):
                meta["entity_keys"] = ",".join(sorted(str(k) for k in metadata["entities"].keys()))[:256]
            await asyncio.to_thread(self._episodic.upsert, ids=[doc_id], documents=[doc], metadatas=[meta])
        except Exception as ex:
            logger.warning("存储单轮情景记忆失败: %s", type(ex).__name__)

    async def _get_profile(self, user_id: str) -> Dict[str, Any]:
        """获取用户画像（取最新一条）。"""
        try:
            doc_id = self._profile_doc_id(user_id)
            direct = await asyncio.to_thread(self._profile.get, ids=[doc_id])
            if direct.get("documents"):
                return json.loads(direct["documents"][0])

            results = await asyncio.to_thread(self._profile.get, where={"user_id": user_id})
            return self._latest_profile_from_results(results)
        except Exception:
            pass
        return {}

    async def ping(self) -> None:
        """确认 Redis 可达；锁和幂等依赖它，不可达时调用方应拒绝启动。"""
        await self._redis.ping()

    async def close(self) -> None:
        """关闭异步 Redis 连接。"""
        await self._redis.aclose()
        await close_model(self._llm)

    @classmethod
    def _conversation_id(cls, user_id: str, conv_id: str) -> str:
        return hashlib.sha256(json.dumps([cls._safe_text(user_id), cls._safe_text(conv_id)], ensure_ascii=True).encode()).hexdigest()

    @classmethod
    def _wm_key(cls, user_id: str, conv_id: str) -> str:
        return f"wm_v2:{cls._conversation_id(user_id, conv_id)}"

    @classmethod
    def _summary_key(cls, user_id: str, conv_id: str) -> str:
        return f"summary_v2:{cls._conversation_id(user_id, conv_id)}"

    @classmethod
    def _state_key(cls, user_id: str, conv_id: str) -> str:
        return f"state_v1:{cls._conversation_id(user_id, conv_id)}"

    @classmethod
    def _profile_doc_id(cls, user_id: str) -> str:
        return f"{cls.PROFILE_DOC_PREFIX}{user_id}"

    @staticmethod
    def _safe_text(value: Any) -> str:
        """转成 ChromaDB 可接受的普通 UTF-8 字符串。"""
        if value is None:
            return ""
        if not isinstance(value, str):
            value = str(value)
        return value.encode("utf-8", errors="ignore").decode("utf-8")

    async def _query_episodic(
        self,
        query_text: str,
        n_results: int,
        where: Dict[str, Any],
    ) -> Dict[str, Any]:
        count = await asyncio.to_thread(self._episodic.count)
        if count <= 0:
            return {"documents": [], "metadatas": []}
        return await asyncio.to_thread(
            self._episodic.query,
            query_texts=[query_text],
            n_results=min(n_results, count),
            where=where,
        )

    @classmethod
    def _rank_memory_rows(cls, results: Dict[str, Any], max_tokens: int, entities: Optional[Dict[str, Any]] = None) -> List[str]:
        docs = results.get("documents") or []
        metas = results.get("metadatas") or []
        docs = docs[0] if docs and isinstance(docs[0], list) else docs
        metas = metas[0] if metas and isinstance(metas[0], list) else metas
        rows = []
        for idx, doc in enumerate(docs):
            if not isinstance(doc, str) or not doc.strip():
                continue
            meta = metas[idx] if idx < len(metas) and isinstance(metas[idx], dict) else {}
            if str(meta.get("superseded", "False")).lower() == "true":
                continue
            requested_keys = {str(key) for key, value in (entities or {}).items() if value}
            indexed_keys = {key for key in str(meta.get("entity_keys", "")).split(",") if key}
            if requested_keys and indexed_keys and not requested_keys.intersection(indexed_keys):
                continue
            try:
                importance = float(meta.get("importance", 0.5))
            except (TypeError, ValueError):
                importance = 0.5
            try:
                updated = datetime.fromisoformat(str(meta.get("ts", "")).replace("Z", "+00:00"))
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                age_days = max(0.0, (datetime.now(timezone.utc) - updated).total_seconds() / 86400)
                importance *= 0.5 ** (age_days / max(1.0, cls.MEMORY_HALF_LIFE_DAYS))
            except (TypeError, ValueError):
                pass
            rows.append((importance, doc))
        rows.sort(key=lambda item: item[0], reverse=True)
        return cls._budget_texts([doc for _, doc in rows], max_tokens)

    @staticmethod
    def _budget_texts(values: List[str], max_tokens: int) -> List[str]:
        budget = max(128, int(max_tokens))
        used = 0
        output = []
        for value in values:
            cost = max(1, len(value) // 2)
            if output and used + cost > budget:
                break
            output.append(value)
            used += cost
        return output

    @staticmethod
    def _dedupe_texts(values: List[str]) -> List[str]:
        seen = set()
        deduped: List[str] = []
        for value in values:
            text = value.strip()
            if not text or text in seen:
                continue
            seen.add(text)
            deduped.append(text)
        return deduped

    async def _merge_summary(self, old_summary: str, new_summary: str) -> str:
        old_summary = self._safe_text(old_summary).strip()
        new_summary = self._safe_text(new_summary).strip()
        if not old_summary:
            return new_summary[: self.SUMMARY_MAX_CHARS]
        if not new_summary:
            return old_summary[: self.SUMMARY_MAX_CHARS]

        prompt = self._safe_text(
            f"""你是对话摘要器。请把下面两段摘要合并为一段不超过 {self.SUMMARY_MAX_CHARS} 个中文字符的摘要。
保留：用户偏好、关键实体、待办事项、约束条件、未解决问题。
只输出摘要正文，不要编号，不要解释。

旧摘要:
{old_summary}

新增摘要:
{new_summary}
"""
        )
        try:
            resp = await complete_text(self._llm, prompt, max_tokens=256, temperature=0.0)
            merged = self._safe_text(resp.text).strip()
            if merged:
                return merged[: self.SUMMARY_MAX_CHARS]
        except Exception as ex:
            logger.warning(f"合并摘要失败，回退为截断拼接: {ex}")

        merged = self._safe_text(f"{old_summary}\n{new_summary}").strip()
        return merged[-self.SUMMARY_MAX_CHARS :]

    @staticmethod
    def _latest_profile_from_results(results: Dict[str, Any]) -> Dict[str, Any]:
        documents = results.get("documents") or []
        metadatas = results.get("metadatas") or []
        if not documents:
            return {}

        candidates: List[tuple[str, Dict[str, Any], str]] = []
        for idx, doc in enumerate(documents):
            if not doc:
                continue
            metadata = metadatas[idx] if idx < len(metadatas) and isinstance(metadatas[idx], dict) else {}
            ts = str(metadata.get("updated_at") or metadata.get("ts") or "")
            candidates.append((ts, metadata, doc))

        if not candidates:
            return {}

        candidates.sort(key=lambda item: item[0], reverse=True)
        latest_doc = candidates[0][2]
        try:
            return json.loads(latest_doc)
        except Exception:
            return {}
