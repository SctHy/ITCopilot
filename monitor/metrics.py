"""IT-Copilot Prometheus 指标：在事件发生处埋点，统一注册在默认 REGISTRY，由 API 的 /metrics 暴露。

命名约定：it_copilot_<对象>_<度量>_<单位>；标签只用低基数取值（Agent 类型、工具名、状态枚举），
不放 user_id / request_id 等高基数字段。
"""
from prometheus_client import Counter, Gauge, Histogram

LATENCY_BUCKETS = (.1, .25, .5, 1, 2, 5, 10, 30, 60)
TOOL_LATENCY_BUCKETS = (.005, .01, .05, .1, .25, .5, 1, 2, 5, 10, 30)

# ── 对话入口 ──────────────────────────────────────────────────────────────────
CHAT_REQUESTS = Counter("it_copilot_chat_requests_total", "IT-Copilot chat requests", ["status", "intent_group"])
CHAT_LATENCY = Histogram("it_copilot_chat_latency_seconds", "IT-Copilot chat latency", ["status"], buckets=LATENCY_BUCKETS)

# ── Agent 与 LLM ──────────────────────────────────────────────────────────────
AGENT_REQUESTS = Counter("it_copilot_agent_requests_total", "Agent 处理次数", ["agent", "status", "error_code"])
AGENT_LATENCY = Histogram("it_copilot_agent_latency_seconds", "Agent 单次处理耗时", ["agent"], buckets=LATENCY_BUCKETS)
AGENT_FAILURES = Counter("it_copilot_agent_failures_total", "Agent 失败原因", ["agent", "reason"])
AGENT_DEGRADED = Gauge("it_copilot_agent_degraded", "Agent 是否处于降级冷却（1=降级）", ["agent"])
LLM_TOKENS = Counter("it_copilot_llm_tokens_total", "LLM token 消耗", ["agent", "model", "direction"])
AGENT_TOOL_CALLS = Counter("it_copilot_agent_tool_calls_total", "Agent 发起的工具调用", ["agent", "tool", "outcome"])

# ── 工具治理层（MCPToolManager） ─────────────────────────────────────────────
TOOL_CALLS = Counter("it_copilot_tool_calls_total", "治理层工具调用结果", ["tool", "outcome"])
TOOL_LATENCY = Histogram("it_copilot_tool_latency_seconds", "治理层工具执行耗时（不含缓存命中）", ["tool"], buckets=TOOL_LATENCY_BUCKETS)
TOOL_CIRCUIT_STATE = Gauge("it_copilot_tool_circuit_state", "熔断状态：0=closed 1=half_open 2=open", ["tool"])
CIRCUIT_STATE_VALUES = {"closed": 0, "half_open": 1, "open": 2}

# ── 意图、知识库与审批 ────────────────────────────────────────────────────────
INTENT_RECOGNITIONS = Counter("it_copilot_intent_recognitions_total", "意图识别次数", ["source", "intent_group"])
INTENT_CLARIFICATIONS = Counter("it_copilot_intent_clarifications_total", "需要澄清的意图识别次数", ["intent_group"])
INTENT_INHERITED = Counter("it_copilot_intent_inherited_total", "追问继承上一轮意图组的次数", ["intent_group"])
KNOWLEDGE_SEARCHES = Counter("it_copilot_knowledge_searches_total", "Agent 知识库检索结果", ["outcome"])
APPROVALS = Counter("it_copilot_approvals_total", "写操作审批事件", ["event"])

# ── 外部 MCP 与监控自身 ───────────────────────────────────────────────────────
MCP_SERVER_UP = Gauge("it_copilot_mcp_server_up", "外部 MCP 服务连接状态（1=connected）", ["server"])
MCP_TOOLS = Gauge("it_copilot_mcp_tools", "外部 MCP 服务已注册的工具数", ["server"])
ALERTS_FIRING = Gauge("it_copilot_alerts_firing", "进程内告警是否触发（1=firing）", ["alert", "severity"])
SLO_BURN_RATE = Gauge("it_copilot_slo_burn_rate", "SLO 错误预算燃烧率", ["slo", "window"])
