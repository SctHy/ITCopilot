"""
亮点：多轮对话记忆管理

三级记忆架构，模拟人类记忆机制：
  1. 工作记忆（Redis）—— 当前会话的最近 N 条消息，毫秒级读写
  2. 情景记忆（向量表）—— 跨会话的历史对话，按语义相似度检索
  3. 用户画像（向量表）—— 从对话中提炼的长期偏好和实体

关键设计：
  - 上下文构建时三级记忆融合，按重要性 + 时效性排序
  - 工作记忆超过阈值时自动压缩（LLM 摘要），防止 context 爆炸
  - Embedding 由本地中文模型生成（core/embedding.py），LLM 用于摘要和画像提炼
"""
import asyncio
import hashlib
import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

import redis.asyncio as redis
from pydantic import BaseModel, ConfigDict
from pydantic_ai import Agent
from pydantic_ai.output import ToolOutput
from pydantic_ai.settings import ModelSettings
from redis.exceptions import WatchError

from core.concurrency import KeyedLocks, redis_lease
from memory.states import profile_phase, COMPRESSION, MAINTENANCE
from core.llm import build_model, close_model, complete_text
from core.vector_store import VectorStore

logger = logging.getLogger(__name__)


class ProfileDoc(BaseModel):
    """员工画像：一份简短的员工说明，像 AGENTS.md 一样整份修订，不逐条追加。写入前仍经 _normalize_profile 脱敏和限长。"""
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)
    background:    str = ""   # 部门/团队、岗位、资历
    work:          str = ""   # 负责的项目、常用系统及用途
    environment:   str = ""   # 公司设备、操作系统、手机
    communication: str = ""   # 语言、希望的回答风格


PROFILE_SCHEMA_VERSION = 4
PROFILE_FIELD_MAX_CHARS = 80

_PROFILE_AGENT = Agent(output_type=ToolOutput(ProfileDoc, name="save_profile", description="保存修订后的员工画像"), retries=0, name="profile_extractor")


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


PROFILE_LABELS = (("background", "背景"), ("work", "工作内容"), ("environment", "设备与环境"), ("communication", "沟通偏好"))


def profile_text(profile: Optional[Dict[str, Any]]) -> str:
    """画像进提示词的样子：每个字段一行，整份通常一两百字。"""
    profile = profile or {}
    return "\n".join(f"{label}：{profile[key]}" for key, label in PROFILE_LABELS if profile.get(key))


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

    def history_turns(self, limit: int = 20, assistant_chars: int = 600) -> List[Dict[str, str]]:
        """工作记忆里的全部对话，供 Agent 作多轮消息历史。

        工作记忆本身有界：攒到 COMPRESS_AT 条就把较早的部分压成摘要、只留最近 5 条，所以这里不再另截一刀；
        否则“最近 N 条之外、尚未压缩”的消息既不在历史里也不在摘要里（情景记忆也不检索当前会话）。

        助手回答按固定长度截断：既压住上下文（单条回答可达上千 token），截断结果又是确定的，
        相邻两轮请求的历史前缀保持一致，模型服务的前缀缓存才能命中。
        """
        turns = []
        messages = self.recent_messages
        if len(messages) > limit:
            # 只在压缩失败（摘要超时/出错会推迟压缩）时走到这里。按 10 条一块截断而不是逐轮滑动：
            # 逐轮滑动让历史的第一条每轮都变，前缀缓存只能命中系统提示和工具定义（实测命中率 0.7→0.3）。
            step = 10
            drop = -(-(len(messages) - limit) // step) * step  # 超出部分向上取整到整块
            messages = messages[drop:]
        for m in messages:
            if m.role not in (MsgRole.USER, MsgRole.ASSISTANT):
                continue
            content = self._clean(m.content)
            if m.role == MsgRole.ASSISTANT and len(content) > assistant_chars:
                content = content[:assistant_chars] + "…（已截断）"
            if m.role == MsgRole.ASSISTANT and (m.metadata.get("tools") or m.metadata.get("approval_id")):
                note = "本轮工具：" + ("、".join(m.metadata.get("tools") or []) or "无")
                if m.metadata.get("approval_id") and m.metadata.get("awaiting_confirmation"):
                    # 草稿还没进入审批：不能写成“已生成待审批”，否则后续回合会被当作已经提交过。
                    note += f"；已生成申请草稿 {m.metadata['approval_id']}（待用户确认，尚未提交审批）"
                elif m.metadata.get("approval_id"):
                    note += f"；已生成待审批 {m.metadata['approval_id']}"
                content += f"\n[{note}]"
            turns.append({"role": m.role.value, "content": content})
        return turns

    def to_prompt_text(self, include_recent: bool = True) -> str:
        """将记忆上下文格式化为 LLM 可用的文本；最近对话改走多轮消息时传 include_recent=False。"""
        parts = []
        if self.summary:
            parts.append(f"[会话摘要]\n{self._clean(self.summary)}")
        if self.relevant_history:
            parts.append("[相关历史]\n" + "\n".join(f"- {self._clean(h)}" for h in self.relevant_history[:3]))
        profile = profile_text(self.user_profile)
        if profile:
            parts.append(f"[用户画像]\n{self._clean(profile)}")
        if include_recent and self.recent_messages:
            parts.append("[最近对话]")
            for m in self.recent_messages[-8:]:
                parts.append(f"{m.role.value}: {self._clean(m.content)}")
        return "\n\n".join(parts)


class MemoryManager:
    """
    三级记忆管理器。

    工作记忆存 Redis（TTL 24h），情景记忆和用户画像存应用数据库的向量表（持久化）。
    """

    WORKING_MAX   = 20    # 工作记忆最大条数，超过则触发压缩
    COMPRESS_AT   = 15    # 达到此条数时压缩，保留摘要 + 最近 5 条
    HISTORY_TOP_K = 5     # 情景记忆检索返回条数
    MEMORY_MAX_TOKENS = 1200
    MEMORY_HALF_LIFE_DAYS = 30.0
    MEMORY_MAX_DISTANCE = 0.6
    SUMMARY_MAX_CHARS = 800
    PROFILE_EVERY_MESSAGES = 6  # 约 3 轮对话提炼一次画像
    PROFILE_DOC_PREFIX = "user_profile:"

    def __init__(
        self,
        redis_url:    str = "redis://localhost:6379/0",
        database_url: str = "sqlite:///data/it_copilot.sqlite3",
        api_key:      str = "",
        base_url:     Optional[str] = None,
        model:        str = "",
    ):
        self._llm   = build_model(api_key, base_url, model)

        self._redis = redis.from_url(redis_url, decode_responses=True)
        self._request_locks = KeyedLocks()
        self.context_mode = os.getenv("IT_COPILOT_MEMORY_MODE", "full")
        if self.context_mode not in {"working", "state", "full"}:
            raise ValueError("unknown memory mode")

        # 情景记忆与用户画像存在应用数据库的向量表（PostgreSQL + pgvector / 本地 SQLite）
        store = VectorStore(database_url)
        self._episodic = store.collection("episodic")
        # 用户画像：存储提炼出的偏好和实体
        self._profile  = store.collection("user_profile")

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
        def raw(role, content, **extra):
            return json.dumps({"role": role, "content": self._safe_text(content),
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
        flow = MAINTENANCE.start()
        await self._flush_archives(user_id, conv_id)
        # _compress 的快照与 WATCH 提交已保护一致性；生成摘要不占聊天锁。
        # 先判断是否该提炼画像：压缩会把工作记忆截短，之后再数就会漏掉这批消息。
        due_at = await self._profile_due(user_id, conv_id)
        flow.send("compress")
        await asyncio.wait_for(self._compress(user_id, conv_id), 30)
        # 提炼成功才推进计数；失败（超时、拿不到租约）时这批消息下一轮还会再试。
        if due_at is None:
            return flow.send("skip")
        flow.send("extract")
        if await self.update_profile(user_id, conv_id):
            await self._redis.set(self._profile_mark_key(user_id, conv_id), due_at, ex=86400)
            return flow.send("commit")
        return flow.send("defer")

    async def _profile_due(self, user_id: str, conv_id: str) -> Optional[int]:
        """画像提炼要调一次 LLM：本会话新增够 PROFILE_EVERY_MESSAGES 条消息才做。到期时返回当前条数，否则 None。"""
        count = await self._redis.llen(self._wm_key(user_id, conv_id))
        mark = int(await self._redis.get(self._profile_mark_key(user_id, conv_id)) or 0)
        return count if profile_phase(count, mark, self.PROFILE_EVERY_MESSAGES) == "due" else None

    async def update_profile(self, user_id: str, conv_id: str) -> bool:
        digest = hashlib.sha256(self._safe_text(user_id).encode()).hexdigest()
        async with redis_lease(self._redis, f"lock:profile:{digest}"):
            return await self._update_profile(user_id, conv_id)

    async def _update_profile(self, user_id: str, conv_id: str) -> bool:
        """
        从当前工作记忆中提炼用户偏好，更新用户画像。
        用 LLM 提炼偏好，然后写入用户画像集合。
        """
        user_id = self._safe_text(user_id)
        conv_id = self._safe_text(conv_id)
        messages = await self._get_working_memory(user_id, conv_id)
        if not messages:
            return False

        current_profile = await self._get_profile(user_id)

        # 只给员工自己说的话：助手回答的风格来自系统规则，给了模型会被当成员工的偏好写进画像。
        said = [m.content for m in messages[-10:] if m.role == MsgRole.USER]
        if not said:
            return False
        text = self._safe_text("\n".join(f"- {c}" for c in said))
        prompt = f"""你在维护一份员工画像，供 IT 服务台下次接待这位员工时参考。像维护 AGENTS.md 一样：简明扼要，只写长期稳定的信息；在旧版本上整份修订，不是追加。
通过 save_profile 返回修订后的完整画像，四个字段：
- background 背景：部门/团队、岗位、资历（如"platform 组后端工程师，新入职"）
- work 工作内容：负责的项目、常用系统及用途
- environment 设备与环境：公司设备、操作系统、手机
- communication 沟通偏好：员工明确提出的要求（如"用中文""简短点"），或从员工自己的写法能明确看出的（如一直用英文写）
规则：
- 每个字段一句话，不超过 60 字；只写员工说过的，不推测；没有依据的字段留空，不写"未知""未提及"。
- 与旧版本冲突时以员工最新的说法为准，过时的内容删掉；旧版本仍然成立的照抄。
- 不写故障、报错、工单、审批这类一次性事务，它们由会话记录负责；不写密码、OTP、恢复码、私钥、token 或任何秘密。

旧版本:
{profile_text(current_profile) or "（空）"}

员工最近说的话:
{text}"""
        prompt = self._safe_text(prompt)

        try:
            result = await asyncio.wait_for(_PROFILE_AGENT.run(
                prompt, model=self._llm, model_settings=ModelSettings(max_tokens=400, temperature=0.0),
            ), timeout=15)
            profile_data = self._merge_profile(current_profile, self._normalize_profile(result.output.model_dump()))

            doc_id = self._profile_doc_id(user_id)
            doc_text = self._safe_text(json.dumps(profile_data, ensure_ascii=False))

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
            return True
        except Exception as ex:
            logger.warning(f"更新用户画像失败: {ex}")
            return False

    @staticmethod
    def _normalize_profile(value: Any) -> Dict[str, Any]:
        """只留四个字段，限长、脱敏，防止任意模型输出或秘密写入长期记忆。"""
        if not isinstance(value, dict):
            return {}
        blocked = ("password", "passwd", "otp", "token", "secret", "private_key", "恢复码")
        output: Dict[str, Any] = {"schema_version": PROFILE_SCHEMA_VERSION}
        for key, _ in PROFILE_LABELS:
            text = " ".join(str(value.get(key) or "").split())[:PROFILE_FIELD_MAX_CHARS]
            if text:
                output[key] = "[REDACTED]" if any(word in text.lower() for word in blocked) else text
        return output

    @staticmethod
    def _merge_profile(previous: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
        """修订版逐字段替换旧版本。修订版里空着的字段沿用旧值：模型漏写不等于信息作废，
        要改掉过时内容就写新句子（例如换了部门就写新部门）。"""
        merged = {"schema_version": PROFILE_SCHEMA_VERSION}
        for key, _ in PROFILE_LABELS:
            value = incoming.get(key) or (previous or {}).get(key)
            if value:
                merged[key] = value
        return merged

    @classmethod
    def _upgrade_profile(cls, stored: Any) -> Dict[str, Any]:
        """schema 3 的列表式画像（department/systems/devices/preferences…）就地转成四个字段，下次提炼时再由模型修订。"""
        if not isinstance(stored, dict) or not stored:
            return {}
        if stored.get("schema_version") == PROFILE_SCHEMA_VERSION:
            return stored
        def join(*keys):
            return "、".join(str(v) for k in keys for v in (stored.get(k) if isinstance(stored.get(k), list) else [stored.get(k)]) if v)
        return cls._normalize_profile({"background": join("department"), "work": join("systems"),
                                       "environment": join("devices", "recurring_issues"), "communication": join("preferences")})

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
          3. 旧消息存入情景记忆供跨会话检索
          4. 工作记忆只保留最近 5 条
        """
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
        messages = self._messages_from_raw(snapshot)
        to_compress = messages[:-5]
        text = self._safe_text("\n".join(f"{m.role.value}: {m.content}" for m in to_compress))
        prompt = self._safe_text(f"用 2-3 句话总结以下对话的关键信息。错误码、事件 ID、设备号、工单号、系统和角色名等标识原样保留：\n{text}")
        try:
            resp = await complete_text(self._llm, prompt, max_tokens=1024, temperature=0.0)
            summary = self._safe_text(resp.text).strip()
            if not summary or resp.truncated:
                logger.warning("摘要为空或被截断，保留原始会话消息")
                return flow.send("defer")
        except Exception as ex:
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
        """跨会话语义检索情景记忆（本地 embedding）。

        当前会话不在检索范围内：最近几轮已作为多轮消息传给模型，更早的部分有会话摘要，
        再检索一遍只会把同一段问答重复塞进上下文。
        """
        query_text = self._safe_text(query).strip()
        if not query_text:
            return []
        conditions = [{"user_id": self._safe_text(user_id)}, {"conv_id": {"$ne": self._safe_text(conv_id)}}]
        if intent_group:
            conditions.append({"intent_group": self._safe_text(intent_group)})
        try:
            results = await self._query_episodic(query_text, n_results=self.HISTORY_TOP_K, where={"$and": conditions})
            docs = self._rank_memory_rows(results, max_tokens, entities)
            return self._budget_texts(self._dedupe_texts(docs), max_tokens)[: self.HISTORY_TOP_K]
        except Exception as ex:
            logger.warning(f"情景记忆检索失败: {ex}")
            return []

    def _archive_key(self, user_id, conv_id):
        return "memory:archives:" + self._conversation_id(user_id, conv_id)

    ARCHIVE_INDEX = "memory:archives:pending"  # set of archive hashes that still hold unflushed items

    async def flush_pending_archives(self, batch=64):
        """Replay archives whose vector write failed, including conversations with no new messages.

        Reads the pending index instead of SCANning the keyspace. Archives written before the
        index existed are migrated by one SCAN pass that stops for good once it wraps around.
        """
        if not getattr(self, "_archive_scan_done", False):
            cursor, keys = await self._redis.scan(getattr(self, "_archive_cursor", 0), match="memory:archives:*", count=1000)
            self._archive_cursor = cursor
            keys = [k for k in keys if k != self.ARCHIVE_INDEX]
            if keys:
                await self._redis.sadd(self.ARCHIVE_INDEX, *keys)
            self._archive_scan_done = cursor == 0
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
        """将压缩后的对话片段存入情景记忆。"""
        try:
            user_id = self._safe_text(user_id)
            conv_id = self._safe_text(conv_id)
            text = self._safe_text(text)
            summary = self._safe_text(summary)
            doc_id = hashlib.sha256(json.dumps([user_id, conv_id, text, summary]).encode()).hexdigest()
            await asyncio.to_thread(
                self._episodic.upsert,
                ids=[doc_id],
                documents=[summary],
                metadatas=[{"user_id": user_id, "conv_id": conv_id,
                            "ts": datetime.now().isoformat(), "full_text": self._safe_text(text[:500])}],
            )
            return True
        except Exception as ex:
            logger.warning(f"存储情景记忆失败: {ex}")
            return False

    async def store_episodic_turn(self, user_id: str, conv_id: str, request_id: str, message: str, response: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Phase 2：每轮将非敏感对话摘要写入情景记忆，支持跨会话语义召回。"""
        try:
            safe_message = self._safe_text(message)[:2000]
            safe_response = self._safe_text(response)[:3000]
            doc = f"员工问题：{safe_message}\n服务台回答：{safe_response}"
            doc_id = hashlib.sha256(f"turn:{request_id}".encode()).hexdigest()
            meta = {"user_id": self._safe_text(user_id), "conv_id": self._safe_text(conv_id), "request_id": self._safe_text(request_id), "ts": datetime.now().isoformat(), "memory_type": "episodic_turn", "importance": 0.5}
            for key, value in (metadata or {}).items():
                if key in {"intent", "intent_group"}:
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
                return self._upgrade_profile(json.loads(direct["documents"][0]))

            results = await asyncio.to_thread(self._profile.get, where={"user_id": user_id})
            return self._upgrade_profile(self._latest_profile_from_results(results))
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
    def _profile_mark_key(cls, user_id: str, conv_id: str) -> str:
        return f"profile_mark_v1:{cls._conversation_id(user_id, conv_id)}"

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
        """转成数据库可接受的普通 UTF-8 字符串。"""
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
        distances = results.get("distances") or []
        docs = docs[0] if docs and isinstance(docs[0], list) else docs
        metas = metas[0] if metas and isinstance(metas[0], list) else metas
        distances = distances[0] if distances and isinstance(distances[0], list) else distances
        # 余弦距离门槛：jina-v2-base-zh 上同主题问答对 0.19–0.62、不同主题 ≥0.62（9 条查询的小样本校准）。
        # 不设门槛时，老用户每轮都会带上 3 条与当前问题无关的旧对话，而且落在不命中缓存的本轮消息里。
        max_distance = float(os.getenv("IT_COPILOT_MEMORY_MAX_DISTANCE") or cls.MEMORY_MAX_DISTANCE)
        rows = []
        for idx, doc in enumerate(docs):
            if not isinstance(doc, str) or not doc.strip():
                continue
            distance = float(distances[idx]) if idx < len(distances) and distances[idx] is not None else None
            if distance is not None and distance > max_distance:
                continue
            meta = metas[idx] if idx < len(metas) and isinstance(metas[idx], dict) else {}
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
            # 相关度为主、重要度与新近度为辅；原先只按重要度排序，相似度顺序被整个丢掉。
            relevance = 1.0 - distance if distance is not None else 0.5
            rows.append((relevance * (0.5 + importance), doc))
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
