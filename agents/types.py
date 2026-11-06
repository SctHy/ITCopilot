"""Agent 运行时共用的数据类型：请求、响应、路由决策与错误。"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from agents.intent.taxonomy import IntentCategory, UrgencyLevel


class AgentType(Enum):
    TRIAGE = "triage"
    ENDPOINT = "endpoint"
    ACCESS = "access"
    NETWORK_SOFTWARE = "network_software"
    TICKET = "ticket"
    GENERALIST = "generalist"   # 单个 Agent 拥有全部工具（默认策略，见 PLANNING_MODES）


# 意图组 → 负责的专职 Agent；未列出的组（general/knowledge）由 Triage 处理。
GROUP_AGENTS: Dict[str, AgentType] = {
    "endpoint": AgentType.ENDPOINT,
    "access": AgentType.ACCESS,
    "network": AgentType.NETWORK_SOFTWARE,
    "software": AgentType.NETWORK_SOFTWARE,
    "ticket": AgentType.TICKET,
    "escalation": AgentType.TICKET,
}


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
    step_outputs: Dict[str, Any] = field(default_factory=dict)
    principal: Any = field(default=None, repr=False)  # core.identity.Principal; set by the API from the verified token
    emit: Any = field(default=None, repr=False)       # async (event, data) -> None; live execution events for SSE
    hidden_tools: frozenset = frozenset()              # tools not offered in this run (e.g. ticket owned by a plan step)
    stream_answer: bool = False                        # push live answer_delta events (single-agent paths only)
    allowed_writes: Optional[frozenset] = None         # write tools the user asked for this turn; None = not gated
    knowledge: List[Dict[str, Any]] = field(default_factory=list)  # knowledge hits prefetched alongside intent

    async def publish(self, event: str, data: Dict[str, Any]) -> None:
        if self.emit is not None:
            await self.emit(event, data)


@dataclass
class AgentResponse:
    agent_type: AgentType
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


@dataclass
class AgentCallResult:
    content: str
    tools_used: List[str] = field(default_factory=list)
    tool_traces: List[Dict[str, Any]] = field(default_factory=list)
    status: str = "success"
    token_usage: Dict[str, int] = field(default_factory=dict)


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
class RoutingDecision:
    primary_agent: AgentType
    supporting_agents: List[AgentType] = field(default_factory=list)
    reason: str = ""
    confidence: float = 0.

    @property
    def agent_types(self) -> List[AgentType]:
        return [self.primary_agent] + self.supporting_agents

    @property
    def multi_agent(self) -> bool:
        return bool(self.supporting_agents)


@dataclass
class OrchestratorResult:
    request_id: str
    response: str
    agent_type: AgentType
    intent: Optional[IntentCategory]
    escalated: bool = False
    latency_ms: float = 0.
    agent_types: List[AgentType] = field(default_factory=list)
    primary_agent: Optional[AgentType] = None
    supporting_agents: List[AgentType] = field(default_factory=list)
    tools_used: List[str] = field(default_factory=list)
    tool_traces: List[Dict[str, Any]] = field(default_factory=list)
    routing_reason: str = ""
    routing_confidence: float = 0.
    status: str = "success"
    retryable: bool = False
    error_code: Optional[str] = None
    plan: Optional[Dict[str, Any]] = None
    findings: List[Dict[str, Any]] = field(default_factory=list)
    trace_events: List[Dict[str, Any]] = field(default_factory=list)
    token_usage: Dict[str, int] = field(default_factory=dict)

    @property
    def trace_id(self) -> str:
        return self.request_id
