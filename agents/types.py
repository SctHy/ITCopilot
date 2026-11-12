"""Agent 运行时共用的数据类型：请求、响应与错误。"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from agents.intent.taxonomy import IntentCategory, UrgencyLevel


@dataclass(frozen=True)
class AgentProfile:
    temperature: float = .2
    max_tokens: int = 3000


@dataclass
class Request:
    message: str
    user_id: str = "anonymous"
    conv_id: str = "default"
    context: str = ""
    history: Optional[List[Dict[str, str]]] = None
    entities: Dict[str, List[str]] = field(default_factory=dict)
    intent: Optional[IntentCategory] = None
    intent_group: Optional[str] = None
    urgency: Optional[UrgencyLevel] = None
    intent_confidence: float = 1.
    department: Optional[str] = None
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    tool_attempts: List[Dict[str, Any]] = field(default_factory=list)
    before_unsafe_tool: Any = field(default=None, repr=False)
    unsafe_tool_calls: set = field(default_factory=set, repr=False)
    conversation_state: Optional[Dict[str, Any]] = None
    step_outputs: Dict[str, Any] = field(default_factory=dict)  # 共享事项回调传入的审批结果等只读证据
    principal: Any = field(default=None, repr=False)  # core.identity.Principal; set by the API from the verified token
    emit: Any = field(default=None, repr=False)       # async (event, data) -> None; live execution events for SSE
    hidden_tools: frozenset = frozenset()              # tools not offered in this run (e.g. writes in a shared-task follow-up)
    stream_answer: bool = False                        # push live answer_delta events
    allowed_writes: Optional[frozenset] = None         # write tools the user asked for this turn; None = not gated
    skill_text: str = ""                               # progressive skill disclosure: bodies selected for this turn
    intent_source: str = "unknown"
    signals: List[str] = field(default_factory=list)
    first_token_ms: Optional[float] = None
    started_at: float = 0.0
    knowledge: List[Dict[str, Any]] = field(default_factory=list)  # knowledge hits prefetched alongside intent
    checkpoints: Any = field(default=None, repr=False)  # scoped async checkpoint port; never passed to tools
    task_references: List[Dict[str, Any]] = field(default_factory=list)  # private-chat recall; not shared-room context
    related_approvals: List[Dict[str, Any]] = field(default_factory=list)  # own withdrawable/releasable items (agents/request_recall.py)

    async def publish(self, event: str, data: Dict[str, Any]) -> None:
        if event == "answer_reset":
            self.signals.append("answer_reset")
        elif event == "answer_delta" and self.first_token_ms is None:
            import time
            self.first_token_ms = (time.monotonic() - self.started_at) * 1000
        if self.emit is not None:
            await self.emit(event, data)


@dataclass
class AgentResponse:
    content: str
    success: bool
    confidence: float = 1.
    latency_ms: float = 0.
    escalate: bool = False
    tools_used: List[str] = field(default_factory=list)
    tool_traces: List[Dict[str, Any]] = field(default_factory=list)
    status: str = "success"
    error_code: Optional[str] = None
    token_usage: Dict[str, int] = field(default_factory=dict)
    observability: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentCallResult:
    content: str
    tools_used: List[str] = field(default_factory=list)
    tool_traces: List[Dict[str, Any]] = field(default_factory=list)
    status: str = "success"
    token_usage: Dict[str, int] = field(default_factory=dict)
    observability: Dict[str, Any] = field(default_factory=dict)


class AgentCallError(RuntimeError):
    """reason 为低基数失败分类，进入 it_copilot_agent_failures_total 指标。"""
    def __init__(self, message, tools_used, tool_traces, reason="exception"):
        super().__init__(message)
        self.tools_used = list(tools_used)
        self.tool_traces = list(tool_traces)
        self.reason = reason


class ModelUnavailableError(AgentCallError):
    def __init__(self, message, tools_used, tool_traces, base_url):
        super().__init__(message, tools_used, tool_traces, reason="model")
        self.base_url = base_url


@dataclass
class AgentRunDeps:
    """PydanticAI 运行依赖：当前请求与本次运行累计的工具证据。"""
    req: Request
    used: List[str] = field(default_factory=list)
    traces: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class OrchestratorResult:
    request_id: str
    response: str
    intent: Optional[IntentCategory]
    escalated: bool = False
    latency_ms: float = 0.
    tools_used: List[str] = field(default_factory=list)
    tool_traces: List[Dict[str, Any]] = field(default_factory=list)
    status: str = "success"
    error_code: Optional[str] = None
    token_usage: Dict[str, int] = field(default_factory=dict)
    observability: Dict[str, Any] = field(default_factory=dict)

    @property
    def trace_id(self) -> str:
        return self.request_id


class TaskTurnFailed(RuntimeError):
    def __init__(self, record):
        super().__init__("Agent 暂时不可用")
        self.record = record
