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
            content = safe_text(m.content)
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
