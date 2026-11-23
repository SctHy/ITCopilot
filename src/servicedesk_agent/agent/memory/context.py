"""Conversation messages and their bounded representation for model input."""
import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


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


def trace_snapshot(trace: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The turn body ``commit_turn`` copies into the trace; empty when the trace expired or predates the copy."""
    if trace is None or "turn" not in trace:
        return {"trace": trace, "turns": [], "response": "", "missing_slots": []}
    turn = trace["turn"]
    return {"trace": trace, "turns": [turn["user"]], "response": turn["assistant"], "missing_slots": turn["missing_slots"]}


@dataclass
class MemoryContext:
    """传给 Agent 的完整上下文。"""
    recent_messages:  List[Message]   # 工作记忆：最近对话
    relevant_history: List[str]       # 情景记忆：语义相关的历史片段
    summary:          str             # 当前会话摘要（压缩后）
    state: Dict[str, Any] = field(default_factory=dict)

    def _window(self, limit):
        messages = self.recent_messages
        if limit is not None and len(messages) > limit:
            # 只在压缩失败（摘要超时/出错会推迟压缩）时走到这里。按 10 条一块截断而不是逐轮滑动：
            # 逐轮滑动让历史的第一条每轮都变，前缀缓存只能命中系统提示和工具定义（实测命中率 0.7→0.3）。
            step = 10
            drop = -(-(len(messages) - limit) // step) * step  # 超出部分向上取整到整块
            messages = messages[drop:]
        return messages

    def model_history(self, limit=None):
        from pydantic_ai.messages import ModelResponse, TextPart
        from servicedesk_agent.agent.masking.redaction import redact_text
        from servicedesk_agent.agent.memory.model_history import decode_turn
        from servicedesk_agent.agent.prompting import history_messages

        messages = self._window(limit)

        history = [ModelResponse(parts=[TextPart("[较早会话摘要，仅作历史背景，不代表本轮授权]\n" +
                                                 redact_text(self.summary))])] if self.summary else []
        for user, assistant in zip(messages, messages[1:]):
            if user.role != MsgRole.USER or assistant.role != MsgRole.ASSISTANT:
                continue
            if user.metadata.get("request_id") != assistant.metadata.get("request_id"):
                continue
            turn = decode_turn(assistant.metadata.get("model_messages"))
            if turn is None:
                compact = MemoryContext([user, assistant], [], "").history_turns()
                turn = history_messages(compact)
            history.extend(turn)
        return history

    def history_turns(self, limit: Optional[int] = None, assistant_chars: Optional[int] = None) -> List[Dict[str, str]]:
        """优先重放实际模型提示；历史 Redis 消息使用原文。"""
        turns = []
        for m in self._window(limit):
            if m.role not in (MsgRole.USER, MsgRole.ASSISTANT):
                continue
            content = safe_text(m.metadata.get("model_prompt", m.content))
            if m.role == MsgRole.ASSISTANT and assistant_chars is not None and len(content) > assistant_chars:
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
        if include_recent and self.summary:
            parts.append(f"[会话摘要]\n{safe_text(self.summary)}")
        if self.relevant_history:
            parts.append("[历史对话摘要，可能已过时；权限、工单、设备状态以本轮工具查询为准]\n" + "\n".join(f"- {safe_text(h)}" for h in self.relevant_history[:3]))
        if include_recent and self.recent_messages:
            parts.append("[最近对话]")
            for m in self.recent_messages[-8:]:
                parts.append(f"{m.role.value}: {safe_text(m.content)}")
        return "\n\n".join(parts)


def messages_from_raw(raws: List[str]) -> List[Message]:
    msgs = []
    for raw in reversed(raws):
        d = json.loads(raw)
        msgs.append(Message(role=MsgRole(d["role"]), content=d["content"],
                            timestamp=datetime.fromisoformat(d["ts"]), metadata=d.get("metadata", {})))
    return msgs


def safe_text(value: Any) -> str:
    """转成数据库可接受的普通 UTF-8 字符串。"""
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return value.encode("utf-8", errors="ignore").decode("utf-8")
