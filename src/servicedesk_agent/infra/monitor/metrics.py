"""ServiceDesk Agent Prometheus 监控指标：统一注册在默认 REGISTRY，由 /metrics 接口对外提供。

命名格式：servicedesk_agent_<模块>_<指标>_<单位>。
所有标签只使用固定枚举值（如工具名、状态），禁止放 user_id / request_id 等无限增长的字段。
"""
from prometheus_client import Counter, Gauge, Histogram

LATENCY_BUCKETS = (.1, .25, .5, 1, 2, 5, 10, 30, 60)
TOOL_LATENCY_BUCKETS = (.005, .01, .05, .1, .25, .5, 1, 2, 5, 10, 30)

# ── 对话入口 ──────────────────────────────────────────────────────────────────
CHAT_REQUESTS = Counter("servicedesk_agent_chat_requests_total", "ServiceDesk Agent chat requests", ["status", "intent_group"])
CHAT_LATENCY = Histogram("servicedesk_agent_chat_latency_seconds", "ServiceDesk Agent chat latency", ["status"], buckets=LATENCY_BUCKETS)
# 一轮对话按顺序经过的阶段：memory_context → intent → agent → approval → commit；agent 内的模型与工具耗时见 AGENT_* / TOOL_*。
CHAT_STAGE_LATENCY = Histogram("servicedesk_agent_chat_stage_seconds", "一轮对话各阶段耗时", ["stage"], buckets=LATENCY_BUCKETS)

# ── Agent 与 LLM ──────────────────────────────────────────────────────────────
AGENT_REQUESTS = Counter("servicedesk_agent_agent_requests_total", "Agent 处理次数", ["status", "error_code"])
AGENT_LATENCY = Histogram("servicedesk_agent_agent_latency_seconds", "Agent 单次处理耗时", buckets=LATENCY_BUCKETS)
AGENT_FAILURES = Counter("servicedesk_agent_agent_failures_total", "Agent 失败原因", ["reason"])
AGENT_CLAIM_CHECKS = Counter("servicedesk_agent_agent_claim_checks_total", "回答与本轮写操作结果不一致的处理：corrected 让模型纠正一次，noted 纠正后仍不一致、追加说明", ["outcome"])
LLM_TOKENS = Counter("servicedesk_agent_llm_tokens_total", "LLM token 消耗（direction=cache_read 为命中模型服务前缀缓存的输入）", ["model", "direction"])
CACHE_LOOKUPS = Counter("servicedesk_agent_cache_lookups_total", "应用内缓存查询（进程内，多进程不共享）", ["cache", "result"])
AGENT_TOOL_CALLS = Counter("servicedesk_agent_agent_tool_calls_total", "Agent 发起的工具调用", ["tool", "error_class"])

# ── 工具治理层（ToolManager） ─────────────────────────────────────────────
TOOL_CALLS = Counter("servicedesk_agent_tool_calls_total", "治理层工具调用结果", ["tool", "outcome"])
TOOL_LATENCY = Histogram("servicedesk_agent_tool_latency_seconds", "治理层工具执行耗时（不含缓存命中）", ["tool"], buckets=TOOL_LATENCY_BUCKETS)
TOOL_CIRCUIT_STATE = Gauge("servicedesk_agent_tool_circuit_state", "熔断状态：0=closed 1=half_open 2=open", ["tool"])
CIRCUIT_STATE_VALUES = {"closed": 0, "half_open": 1, "open": 2}

# ── 意图、知识库与审批 ────────────────────────────────────────────────────────
INTENT_RECOGNITIONS = Counter("servicedesk_agent_intent_recognitions_total", "意图识别次数", ["source", "intent_group"])
INTENT_CLARIFICATIONS = Counter("servicedesk_agent_intent_clarifications_total", "需要澄清的意图识别次数", ["intent_group"])
INTENT_INHERITED = Counter("servicedesk_agent_intent_inherited_total", "追问继承上一轮意图组的次数", ["intent_group"])
KNOWLEDGE_SEARCHES = Counter("servicedesk_agent_knowledge_searches_total", "Agent 知识库检索结果", ["outcome"])
APPROVALS = Counter("servicedesk_agent_approvals_total", "写操作审批事件", ["event"])
AUTH_FAILURES = Counter("servicedesk_agent_auth_failures_total", "鉴权/授权拒绝", ["reason"])
RETENTION_DELETED = Counter("servicedesk_agent_retention_deleted_total", "按保留期删除的行", ["kind"])
RATE_LIMITED = Counter("servicedesk_agent_rate_limited_total", "限流拒绝的请求（bucket 为 agent_turn 按身份、agent_turn:tenant 按租户）", ["bucket", "channel"])

# ── 后台任务执行（Worker） ───────────────────────────────────────────────────
WORKFLOW_ACTIONS = Counter("servicedesk_agent_workflow_actions_total", "后台任务状态变化次数", ["kind", "outcome"])
GRANT_DRIFT = Counter("servicedesk_agent_grant_drift_total", "定时核对发现外部权限不一致的次数", ["system"])
RECONCILE_SKIPPED = Counter("servicedesk_agent_reconcile_skipped_total", "外部系统暂时不可用而跳过的定时核对次数")
WORKFLOW_OLDEST_AGE = Gauge("servicedesk_agent_workflow_oldest_age_seconds", "等待最久的未处理任务耗时（秒）", ["state"])
CONFIG_RELOADS = Counter("servicedesk_agent_config_reloads_total", "配置热加载结果：applied 生效，rejected 校验失败沿用旧配置", ["config", "outcome"])
POLICY_REVALIDATIONS = Counter("servicedesk_agent_policy_revalidations_total", "策略变更后重新校验处理中审批与授权的结果", ["outcome"])

# user_id、tenant、approval_id、action_id、conv_id 和 request_id 只记录在日志中，不放进指标标签。
WORKFLOW_WAKEUPS = Counter("servicedesk_agent_workflow_wakeups_total", "Worker 被唤醒的次数（按通知或轮询分类）", ["source"])
WORKFLOW_RELEASES = Counter("servicedesk_agent_workflow_leases_released_total", "主动归还任务租约的次数", ["reason"])
MANUAL_RESOLUTIONS = Counter("servicedesk_agent_workflow_manual_resolutions_total", "管理员人工处理异常任务的次数", ["resolution"])
DISPATCH_LATENCY = Histogram("servicedesk_agent_workflow_dispatch_latency_seconds", "任务从可执行到被领取的调度耗时（秒）", buckets=TOOL_LATENCY_BUCKETS)

# ── 实时事件流 ────────────────────────────────────────────────────────────────
STREAM_FIRST_EVENT = Histogram("servicedesk_agent_stream_first_event_seconds", "SSE 收到首个进度事件的延迟（秒）", buckets=TOOL_LATENCY_BUCKETS)


# 数据脱敏统计：只统计次数和耗时，不记录原值、占位符、租户或会话 ID。
MODEL_MASK_REQUESTS = Counter("servicedesk_agent_model_mask_requests_total", "发给模型前的脱敏处理结果", ["outcome"])
MODEL_MASK_VALUES = Counter("servicedesk_agent_model_mask_values_total", "敏感信息替换次数", ["kind"])
MODEL_MASK_LATENCY = Histogram("servicedesk_agent_model_mask_seconds", "脱敏处理耗时（秒）", buckets=(.0001, .0005, .001, .005, .01, .05, .1, .5, 1))
LOCAL_PII_REQUESTS = Counter("servicedesk_agent_local_pii_requests_total", "本地脱敏模型检测结果", ["outcome"])
LOCAL_PII_LATENCY = Histogram("servicedesk_agent_local_pii_seconds", "本地脱敏模型检测耗时（秒）", buckets=(.001, .005, .01, .05, .1, .5, 1, 5, 10))
