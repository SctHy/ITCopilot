# Changelog

## v3 — 可信身份、可恢复执行与实时事件流

- 身份：请求身份只取自已验证 JWT（OIDC/Keycloak 或开发令牌），请求体身份字段被忽略；RBAC 四角色；trace、审批、审计、员工状态、事件流按归属授权；`client_request_id` 按调用者隔离，修复不同用户同 ID 时 trace 互相覆盖、审批冲突的问题。移除共享管理员令牌。
- 策略与审批：`config/access_policy.yaml` 版本化策略（可申请的系统/角色、审批人数、最长有效期、代申请角色），工具与审批库双重校验；职责分离；developer 权限需两名审批人，投票做版本号 CAS。
- 执行：新增 `workflow/`（SQLAlchemy + Alembic，SQLite/PostgreSQL）与 `adapters/`（Gitea、本地目录）；审批通过同事务写入 outbox，租约 worker 执行；确定失败/可重试/结果未知三分类，结果未知先回查；baseline 先持久化；到期自动回收并做漂移检查；批量失败 saga 补偿；管理员对账接口。替换 `tooling/business_store.py`。
- 实时事件流：执行事件写入 Redis Streams，SSE 实时推送、`Last-Event-ID` 续传，执行与连接解耦。
- 统一工具执行层：所有 Agent 工具经超时、熔断、只读重试与写操作幂等键。
- 检索：向量召回在部门过滤后命中不足时自适应扩窗，修复其他部门文档挤占候选导致返回为空的问题。
- 部署：compose 增加 postgres / keycloak / gitea（含初始化任务）/ 独立 worker；修复镜像构建时 `data/` 目录权限导致构建失败；前端 OIDC 登录（PKCE）、演示身份切换、审批页、实时事件展示。
- 质量：GitHub Actions（lint、离线测试、PostgreSQL、Keycloak+Gitea 集成、前端）；21 项安全回归、真实 TCP 流式回归；后端 183 项测试（另 3 项真实服务集成测试按需运行），前端 18 项。见 docs/evaluation-v3.md、docs/adr/、docs/threat-model.md。
- 复合诊断建单：计划 token 预算默认 30000（`IT_COPILOT_PLAN_TOKEN_BUDGET`）；Endpoint/Network/Access 开放仅生成审批的 `create_ticket`；模型轮数默认 5（`IT_COPILOT_MAX_MODEL_ROUNDS`）；计划含工单步骤时诊断步骤隐藏建单工具。
- 新增 `generalist` 对照策略与 64 场景分层评测集（`application_strata_v3.json`）、按领域证据与多效果检查、`compare_strategies.py`（Wilson 区间、配对 McNemar）；结果：single 40/64、bounded 44/64、generalist 63/64。
- 真实模型（deepseek-flash）重跑应用评测：开发集 24/24、23/24，留出集 36/36、36/36（v2：23/24、22/24、36/36、33/36）；修复评测脚本工号解析缺陷，报告记录实际上游模型。

## v3.1 — 多 Agent 修复与多轮状态评测

- 多 Agent 规划：新增 LLM 规划器（结构化输出，失败回退规则规划器）；只在用户要求时追加建单步骤；token 预算按步骤数计算（每步 10000）。在第 6 节同一批 42 个场景上，多 Agent 从 26/42 升到 41/42。
- 新增 `bounded_all` 策略（专职 Agent 持有全部工具）；默认策略改为 `generalist`。
- 审批：同一会话、同一申请人的新申请如果完全覆盖某张仍待审批的旧单，旧单标记为 `superseded`，不能再被批准，替代关系写入审计。修复多轮对话中重复建单（修复前占多轮评测失败的 72%）。
- 新增 τ-bench 式多轮评测（`evaluation/simulated.py`、`scripts/evaluate_simulated.py`、`simulated_v4.json`）：组合任务、LLM 模拟用户、按最终状态评分、pass^k、模拟用户越界标记；有测试检查任务可解、“什么都不做”判为失败。
- `compare_strategies.py` 支持按策略、按轮次拆开的报告文件，新增 `--exclude-simulator-flags`、`--exclude-errored`。
- 评测运行后段模型服务额度耗尽，部分分层没有有效数据，详见 docs/evaluation-v3.md 第 7 节。
- 新增 ADR 0006（默认通用 Agent）、0007（待审批申请替代）。后端 200 项测试通过（另 3 项真实服务集成测试按需运行），前端 18 项；工作流测试在 PostgreSQL 17 上通过。

## v2 — 可验证业务执行与对照评测

- 新增 SQLite 本地业务适配器：冻结审批参数和哈希、批准/执行状态分离、幂等操作记录、事务回滚、重启重试与审计。聊天阶段仅准备申请，不再写入会阻塞恢复的旧“结果未知”回执。
- 会话锁覆盖最终状态提交；修正建单误要求 ticket_id、多轮返回字段、权限角色 Schema；前端支持查看参数、批准/拒绝、重试与执行凭据。
- Planner 传递依赖工具证据、阻断失败依赖、波次间检查 token 预算，停止时保留已用 token 和 trace。
- 默认 LLM 意图主路由与规则兜底，保留四策略对照；退休样本内校准，按模型/来源绑定独立 calibration 子集参数。
- 新增真实 HTTP 应用评测、88 条意图消融、60 文档/40 查询 RAG 对照、记忆消融、进程中断和监控故障注入。结果与限制见 docs/evaluation-v2.md；未证明多 Agent 或 Hybrid 总体优于简单基线。
- DeepSeek 强制工具选择通过显式 thinking 配置兼容；保留真实调用失败，不混算兜底准确率。

## Phase 11 — 清理与二次体检

- 修复 Skills 从未生效：手写 front matter 解析把 `[a, b]` 解析成带方括号的字符串，Agent 名永远不匹配，4 个 Skill 从未注入任何提示词；改用 YAML 解析并补测试。
- 修复多轮补槽：缺失字段只看本轮消息，上一轮已给出的工号仍会阻断写操作并被重复追问；现按跨轮累积实体计算，Agent 也会收到累积实体。
- 安全回归门禁中“失败不编造”“部门边界”两项原为硬编码通过，改为调用真实代码路径的检查（并验证违规时会失败）。
- 知识库：检索不再每次全量重建 BM25 索引（按片段数变化与 30s 周期刷新）；RRF 合并去掉线性查找；修复 bm25 模式 `score` 实为向量分、同批次同名文档元数据取自旧版本。
- 删除死代码与过期文件：`core/access.py`、无引用脚本 3 个、过期 Redis 键迁移（每请求多 8 次 Redis 往返）、若干无调用方法、前端 Java 残留配置、冗余 ignore 规则；本地缓存清理。
- 评测脚本抽出公共 `evaluation/reporting.py` 并展开压行；新增 `scripts/evaluate_dialogs.py` 作为 LLM-as-Judge 评测入口；校准报告去掉从未生效的 handoff 阈值。
- 后端离线测试 148 项。

## Phase 10 — 监控闭环、MCP 接入与容器化

- 监控：事件处埋点的 Prometheus 指标（`monitor/metrics.py`）；`PerformanceMonitor` 重写为近期窗口规则 + SLO 多窗口燃烧率 + 告警状态机（迁移时通知、防抖恢复、Webhook）；补全 Agent 降级状态机（冷却 → 半开单探针 → 恢复），降级时由 Triage 兜底。
- 链路追踪：PydanticAI 接入 OpenTelemetry，`/chat` 根 span 下挂模型与工具子 span，默认不记录内容。
- 记忆：接入 `ProfileUpdater`，修复 `/chat` 写入路径下会话压缩从未触发的问题；同一用户的后台维护合并执行。
- MCP：客户端与服务端迁移到 MCP SDK 2.x（此前在 2.x 下无法运行）；外部工具经治理层按白名单动态下发，断开即下线；本地知识库挂载为 `/mcp/`；修正配置中不存在的 Agent 类型。
- API：新增 `/knowledge/add`、`/knowledge/upload`、`/eval/reports`；写操作接口支持 `IT_COPILOT_ADMIN_TOKEN` 鉴权；CORS 来源可配置。
- 前端：移除不存在的 Java 后端与依赖已删除接口的在线评测；“评测”页改为“运行与评测”（实时 SLO、告警、组件健康度 + 离线评测报告）；修复运行状态面板读取旧字段始终为空。
- 部署：新增根目录 `Dockerfile` 与 `docker-compose.yml`（redis / gateway / api / frontend，`observability` profile 含 Prometheus / Grafana / Jaeger），Prometheus 告警规则与 Grafana 仪表盘；删除引用 Java 服务的旧 compose 与 nginx 配置。
- 依赖升级到 pydantic-ai-slim 2.63、pytest 9.2 等最新版本；后端离线测试 130 项，前端 14 项。

## Phase 9 — 代码体检与修复

- 意图识别：去掉针对评测集三条追问用例写死的特判，改为通用追问继承规则（同组保留具体意图、非领域意图继承上一轮组、跨领域视为换话题）。
- 本地 n-gram embedding 由进程随机加盐的 `hash()` 改为 `zlib.crc32`，意图投票与校准指标跨进程可复现。
- LLM 不可用时紧急度改由规则判定（此前恒为 normal）。
- 修复 `/trace/tools` 被 `/trace/{request_id}` 遮蔽而不可达；后台任务保存引用并记录异常；并行 Agent 全部失败时返回明确的 failed 状态。
- 重写 Agent 编排、意图识别、工具定义与 API 入口的分号压行代码；路由由 `INTENT_GROUPS` 推导，消除重复映射；API 不再访问其他模块私有成员。
- 用 17 个真实断言替换 7 个不调用项目代码的空测试；后端离线测试共 92 项。

## Phase 8 — 迁移到 PydanticAI 与 Python 3.14

- Agent 运行时从手写 AsyncAnthropic tool_use 循环迁移到 PydanticAI `Agent`；白名单、Schema 校验、写操作守卫、trace、3 轮上限与错误分类保持不变。
- 意图识别、查询改写、重排、画像提炼改为 `ToolOutput` 结构化输出；摘要与 LLM Judge 使用 `pydantic_ai.direct`。
- 新增 `core/llm.py` 模型工厂与 `IT_COPILOT_LLM_PROVIDER`（anthropic 经网关 / openai 直连）；网关修复 `tool_choice` 一律被改写为 auto 的问题。
- 运行环境升级到 uv 管理的 Python 3.14，全部依赖升级到最新版本；后端离线测试增至 82 项。

## Phase 7 — 工程化与可观测

- 增加 `/chat/stream` SSE 状态流、前端工具/审批状态展示。
- 统一 `/trace/{request_id}` 回放计划、步骤、工具、记忆、token、耗时、fallback 与审批事件。
- 扩充离线后端边界测试至 71 项，保留前端 18 项 node:test。
- 重写项目主页 README，补充架构、指标、生产替换边界、设计取舍和 MIT License。

## Phase 1–6 — IT-Copilot 基础能力

- 基于多智能体编排骨架搭建 IT-Copilot 企业内部 IT 服务台，聚焦账号、权限、网络、设备与工单五类问题。
- 恢复 AsyncAnthropic/tool_use、8082 网关、多 Agent 路由、并行执行与工具可靠性层。
- 完成 Redis 锁与幂等、三级记忆、BM25/vector/hybrid RAG、Planner、HITL 审批和安全评测。
- 完成意图置信度温度校准：47 条固定集 ECE 0.394681→0.258583，Brier 0.168457→0.114710。
