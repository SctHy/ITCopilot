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

## v3.2 — 依赖与镜像升级、删除协议网关

- 删除 8082 Anthropic→OpenAI 协议网关（`gateway/`、compose 的 gateway 服务）：PydanticAI 用 `OpenAIChatModel` 直连 OpenAI-compatible 服务，配置统一为 `.env` 的 `OPENAI_BASE_URL` / `OPENAI_API_KEY` / `OPENAI_MODEL`；原网关为 DeepSeek 关闭 thinking 的逻辑改为 `OPENAI_THINKING_TYPE` 经 `extra_body` 下发。移除 `ANTHROPIC_*` 与 `IT_COPILOT_LLM_PROVIDER`。错误码 `gateway_unavailable` 改为 `model_unavailable`，失败原因标签 `gateway` 改为 `model`（告警 `ITCopilotModelErrors`）。
- requirements 去掉未直接使用的 `sse-starlette`、`pydantic-settings`（分别由 mcp、chromadb 间接引入），`pydantic-ai-slim` 只保留 `openai` extra。
- Python 依赖：pydantic-ai-slim 2.64、pydantic 2.14.2、pydantic-settings 2.16.2、OpenTelemetry 1.45.2、python-multipart 0.0.33。Python 保持 3.14：onnxruntime（Chroma 默认 embedding）尚无 3.15 wheel。
- 前端：Vite 8、@vitejs/plugin-vue 6.0.10、Vue 3.5.44；构建镜像 Node 26.13，CI 的 Node 从 24 升到 26。
- 镜像：PostgreSQL 18、Keycloak 26.8、Gitea 1.27。PG 18 镜像的数据目录改为 `/var/lib/postgresql/18/docker`，compose 改挂 `/var/lib/postgresql` 并换用新卷 `postgres18-data`；旧卷 `postgres-data` 不再使用，需要旧数据时用 `pg_upgrade` 或 dump/restore 迁移。
- 验证：后端 200 项测试通过（3 项真实服务集成测试在 Keycloak 26.8 + Gitea 1.27 上通过）；工作流测试在 PostgreSQL 18 上连续 3 次通过；前端 18 项测试与构建通过。

## v3.3 — 中文 embedding

- 知识库、情景记忆、用户画像的 Chroma 集合改用 fastembed 本地加载 `BAAI/bge-small-zh-v1.5`（`core/embedding.py`，`IT_COPILOT_EMBEDDING_MODEL` 可换），替换 Chroma 默认的英文 all-MiniLM-L6-v2；bge 查询侧加官方检索指令。
- 集合名带模型后缀（如 `knowledge_base__bge-small-zh-v1.5`）；新集合为空时从同名旧集合复制文本与元数据并重新向量化，旧集合保留。
- rag-v2（60 文档 / 40 查询）：vector Recall@3 0.525→0.925、MRR 0.446→0.850；hybrid Recall@3 0.725→1.0、MRR 0.654→0.908；检索延迟约 50ms→5ms。12 条配对子集上普通 hybrid 已达 Recall@3 1.0 / MRR 0.944，LLM 改写重排为 1.0 / 1.0（多约 2 秒）。去掉查询指令的对照：vector MRR 0.875、hybrid Recall@3 0.975，差异在一条查询以内。记忆消融结果不变。
- 镜像构建期预下载模型（`FASTEMBED_CACHE_PATH`），不再预下载 MiniLM。
- Agent 知识检索默认只做一次 hybrid 检索（`MCPToolManager.search`），LLM 查询改写 + 重排改为 `IT_COPILOT_RAG_REWRITE=true` 时启用。rag-v2 全部 40 条：hybrid Recall@3 1.0 / MRR 0.908 / 约 7ms，改写重排 1.0 / 1.0 / 约 2.7 秒，只改善了 7 条的排序。`evaluate_rag_ablation.py --live` 默认在全部查询上对照（`--live-limit` 可截取）。

## v3.4 — 向量存入 PostgreSQL（pgvector），移除 Chroma

- 知识库、情景记忆、用户画像的向量改存应用数据库的 `vector_documents` 表（Alembic 0003，PostgreSQL 上创建 `vector` 扩展）：部署用 pgvector `<=>` 余弦距离在库内检索，本地与测试用 SQLite 存 float32、numpy 计算。`core/vector_store.py` 提供 upsert / get / query / delete / count，过滤支持字符串等值、`$in`、`$and`，在 SQL 里求值。
- 每行记录生成向量的模型；换 `IT_COPILOT_EMBEDDING_MODEL` 后首次打开集合时按新模型重新向量化。
- 知识库部门过滤从“查询后过滤 + 扩窗重查”改为 SQL 召回前过滤；向量名次与 BM25 一样只在可见文档中计算。rag-v2：hybrid Recall@3 1.0→0.975、MRR 0.908→0.900（5 条排序有升有降），vector 与 BM25 不变；SQLite 与 pgvector 结果一致。LLM 改写重排 1.0 / 1.0，约 2.4 秒，仍默认关闭。记忆消融结果不变。
- 移除 chromadb（及 35 个只被它使用的间接依赖）；新增 pgvector、numpy。compose 与 CI 的 Postgres 镜像改为 `pgvector/pgvector:0.8.3-pg18`。
- `CHROMA_PERSIST_DIRECTORY` 改为 `IT_COPILOT_DATA_DIR`（默认 `data`），本地库文件改为 `data/it_copilot.sqlite3`。旧 Chroma 数据用 `scripts/migrate_chroma.py` 一次性迁入（需临时安装 chromadb）。
- 测试的 `db_url` fixture 移到 `tests/conftest.py`，PostgreSQL 下 search_path 带上 public；新增 `tests/test_vector_store.py`，SQLite 与 PostgreSQL 跑同一组用例。

## v3.5 — 分阶段耗时、SQLite 并发读、共用引擎、端到端复测

- 分阶段耗时：新增指标 `it_copilot_chat_stage_seconds{stage}`（memory_context / intent / agent / approval / commit），`ChatResponse.stage_ms` 与 trace `observability.stage_ms` 同步记录；`evaluate_application.py` 汇总每阶段 p50/p95 与占比（`turn_stage_ms`）。
- SQLite 只读查询（`workflow.db.read`）改用 DEFERRED 事务，WAL 下不再与写事务排队；写事务仍 `BEGIN IMMEDIATE`。工作流 8 处读、向量存储全部读走只读连接，有并发测试覆盖。
- 同库共用引擎：`workflow.db.shared_engine` 按 URL 只建一个连接池、只迁移一次（知识库、记忆、工作流共用），弱引用缓存，使用方释放后引擎回收。
- BM25 索引改按集合版本（行数, 最近写入时间）刷新，去掉 30 秒全量重读。
- 去掉三处已不存在的 `claude-3-5-sonnet-20241022` 默认模型名，统一由 `build_model` 回落到 `OPENAI_MODEL`。
- 端到端复测（最新代码，64 场景，generalist，deepseek-flash）：62/64，p50 6.3 s / p95 11.4 s（v3：64/64，7.1 / 12.6）。Agent 占每轮耗时 79%，意图识别 20%（p50 1.2 s）。`n-vpn-developer` 重复 5 次全过；`c3-grant-asset-ticket` 关闭/开启改写重排各 15 次为 10/15、13/15（p=0.39），失败均为模型按 endpoint 技能“先收集系统版本”而不建单，属提示词冲突，未修。报告 `application_strata_v3_pgvector_run1.json`。
- 技能提示词：endpoint 技能的“先收集系统版本”改为“诊断时按需收集，不是建单前提”；ticket 技能补充“建单只需 title/description，缺的信息写进描述；工单本身是待审批记录，用户已要求建单时不在聊天里二次确认”。c3 重复 15 次：修改前 10/15，只改第一条 10/15（失败变为反问“要现在建吗”），两条都改 13/15（p=0.39，不显著）。完整 64 场景两次重跑均 64/64，“不应写入” N 层 10/10、10/10，未见过度写入；p50 6.5 / 6.6 s，p95 12.7 / 12.7 s。报告 `application_strata_v3_skillfix_run1.json`、`_run2.json`。

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

## v3.6 — 飞书入口、告警主动建单、策略变更复核

借鉴 DeepSeek dsh-superagent-prototype 的三个设计：Source/Channel 与“先存储再执行”、推断工作的受理模式、策略变更时复核授权。

- 策略变更复核（ADR 0008）：`load_policy()` 按 mtime 热加载，改坏时沿用上一版；`WorkflowStore.revalidate()` 让旧版本下的待审批单失效（新状态 `invalidated`）或提高审批人数，取消未执行的动作，生效中的授权按新上限提前回收；审批决定前、首个动作执行前各再查一次；worker 发现版本变化时自动复核；新增 `GET /policy`、`POST /policy/revalidate`。修复同一授权被重复复核时到期时间被反复缩短的问题（开发中由测试发现）。
- 飞书入口（ADR 0009）：`POST /channels/feishu/events`（token/签名/解密校验）→ `channel_inbox` → 与 `/chat` 同一路径 → `channel_outbox`；私聊、@、话题内追问三种受理方式；`config/feishu_users.yaml` 身份映射；同一会话逐条处理、出站按序号保序，失败退避，永久失败丢弃并留记录；`WorkflowStore.notify` 钩子在审批状态变化的同一事务内写入通知，API 与独立 worker 都会挂上；`/monitor` 增加 `channels`（只列需要人处理的问题）。
- 告警主动建单（ADR 0010）：`POST /proactive/alerts`（Bearer 共享密钥）、`config/alert_rules.yaml`（恰好命中一条规则、冷却期、`off / observe / auto`）、`proactive_decisions` 决定记录、`GET /proactive/decisions`；auto 模式以服务身份 S0001 提交待审批的建单申请，草稿附告警证据与知识库结果；compose `observability` profile 新增 Alertmanager。
- 迁移 0004：`channel_inbox`、`channel_outbox`、`proactive_decisions`。新增指标 `it_copilot_policy_revalidations_total`、`it_copilot_channel_messages_total`、`it_copilot_channel_deliveries_total`、`it_copilot_proactive_decisions_total`。
- 测试：新增 31 项（策略复核 9、飞书 15、主动受理 7），后端共 239 项通过、3 项真实服务集成测试按需运行；三个新测试文件加入 CI 的 PostgreSQL 任务（本地未在 PostgreSQL 上运行）。

## v3.7 — 结构精简

只调整代码组织，行为不变：离线门禁 `evaluate_all.py` 的各项报告与重构前逐字节一致；后端 239 项测试在 SQLite 和 PostgreSQL 18（pgvector 0.8.3，docker）上都通过。

- `api/main.py` 从 906 行拆为 190 行：`api/state.py`（运行时组件）、`api/deps.py`（身份依赖）、`api/schemas.py`（模型）、`api/routes/`（chat、approvals、knowledge、auth、ops、intake）。测试与评测脚本改为替换 `api.state` 的属性（如 `monkeypatch.setattr(state, "workflow", ...)`），`evaluation.application` 产出 `(client, state)`。
- 模块归位：`core/` 中的意图识别（`intent_recognizer` / `intent_embedding` / `intent_calibration`）、`planner`、`conversation_state` 移到 `agents/`；`core/telemetry.py` → `monitor/`；`core/approval.py` → `workflow/`；`adapters/` → `workflow/adapters/`；`channels/` 与 `proactive/` 合并为 `intake/`（`channels/service.py` → `intake/im.py`，`proactive/alerts.py` → `intake/alerts.py`）。HTTP 路径不变。
- 删除：`scripts/migrate_chroma.py`（Chroma 已移除，迁移是一次性的，需要时可从 git 历史恢复）、`scripts/evaluate_dialogs.py`（`evaluate_application.py` 的别名）；`tooling/schemas.py` 并入 `tooling/knowledge_base.py`，`evaluation/few_shot.py` 并入 `evaluation/evaluator.py`。
- `evaluate_application.py` 的代码指纹改为递归覆盖 `api/`、`agents/`、`core/`、`intake/`、`memory/`、`tooling/`、`workflow/`、`evaluation/`（之前不含子目录，也漏了 `workflow/`）。旧报告里的 `code_sha256` 与新指纹不可比。
- 测试：路由注册检查改为读 OpenAPI 路径（include_router 后 `app.routes` 不再直接列出子路由）。

## v3.8 — 公开基准评测与万级片段检索性能

- 公开基准：新增 `scripts/evaluate_rag_wixqa.py`（WixQA，6,221 篇客服文章、400 条人工查询，固定 revision 与 sha256）和 `scripts/evaluate_agentdojo.py`（AgentDojo v1.2.2 四个套件、949 个间接注入组合，跑应用的 `BaseAgent` 运行时）。结果与边界见 docs/evaluation-public.md。
- 检索性能：BM25 改为 numpy 倒排索引，只展开词法排名前 `top_k*4` 的片段（与全量展开逐条一致）；SQLite 向量矩阵按集合版本与过滤条件缓存，按 id 排序使打平时结果确定；迁移 0005 增加 `(collection, updated_at)` 索引；`reembed_stale` 同时刷新 `updated_at`。WixQA 35,083 片段上混合检索 p50 0.63–0.80 s → 12–14 ms。
- 检索质量：分词去掉词首尾的 `.`、`/`、`-`；切片按中英文句子边界切并保留原文字符。WixQA BM25 Recall@5 0.41 → 0.45；rag_v2 指标不变。
- 测试：新增 5 项（缓存失效、BM25 公式对照、截断展开与全量 RRF 对照、切片、分词），后端 250 项通过（3 项需 PostgreSQL 的用例在 SQLite 下跳过）；CI 的 PostgreSQL 测试组在 PostgreSQL 18 + pgvector 0.8.3（docker）上 51 项通过。

## v3.9 — 收缩范围

删掉不在演示主线上、或评测已证明没有收益的部分。

- 监控：删除 compose 里的 Grafana、Jaeger、Prometheus、Alertmanager 及其配置（`config/grafana/`、`config/prometheus/`、`config/alertmanager/`）。`/metrics`、`/monitor` 的进程内 SLO 告警和可选的 OpenTelemetry 导出保留，追踪可接任意 OTLP 后端。
- 告警主动建单（ADR 0010，改为已废弃）：删除 `intake/alerts.py`、`POST /proactive/alerts`、`GET /proactive/decisions`、`config/alert_rules.yaml`、服务身份 S0001 与相关指标；迁移 0006 删除 `proactive_decisions` 表。
- 意图识别：删除向量匹配（`agents/intent_embedding.py`，含 SiliconFlow 依赖）、三路加权投票与置信度校准（`agents/intent_calibration.py`、`scripts/calibrate_intents.py`、`scripts/evaluate_intent_ablation.py`、`evaluation/config/intent_calibration.json`）。理由：留出集上 LLM 与融合都是 44/44，单路规则 30%、向量 57%，校准默认配置为空。现在是 LLM 结构化输出 + 关键词规则兜底，`IT_INTENT_STRATEGY` 不再使用。历史报告保留。
- 离线门禁的意图识别以前走类默认的融合路径，与线上（LLM 路径）不一致；现在只有一条路径。重新生成的 `intent_latest.json` 预测与准确率不变，21 条兜底样本的置信度由 0.5 变为规则给出的 0.65。
- 测试：删除被移除功能的测试，后端 246 项通过、3 项需 PostgreSQL 的用例在 SQLite 下跳过。
- 技能自改进（ADR 0011，`evaluation/improvement.py`、`scripts/self_improve.py`）：失败归因、模型提案只改技能正文、确定性守卫、基线与候选同轮配对重跑、开发集净提升且单侧 McNemar p ≤ 0.1 才看留出集与安全集；默认不改线上技能。只做过 2 个任务的冒烟运行，没有效果证据。

