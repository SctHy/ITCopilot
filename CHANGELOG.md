# Changelog

## 未发布 — WP7 长期记忆治理

- 统一写入前脱敏；情景记忆只接纳有业务证据的成功回合，以主体级近重复合并刷新记录，保留业务ID与重要度。
- 画像升级schema 5，逐字段记录来源和时间，180天陈旧字段不进入提示词；历史摘要注明以当前工具状态为准。
- 新增本人记忆查看/删除API及同事务审计、旧记忆脱敏重嵌入脚本；20段确定性治理评测20/20，真实模型memory ablation未运行。见ADR0032。



## 未发布 — WP9 成功率波动归因

- 使用持久回合元数据按版本、模型、意图、指纹和 token 分桶，区分失败率变化与流量构成变化；输出 Wilson 区间、工具错误变化、变更时间线和失败样本。
- 新增只读 `scripts/attribute_success_drop.py` 和 [排查手册](docs/runbook-success-drop.md)，方法与限制见 ADR 0034。


## 未发布 — WP2 持久回合与版本指纹

- 迁移 0007 持久保存不含正文的回合元数据、工具错误类别、模型请求数和版本指纹，支持私人对话、飞书和共享事项。
- 新增部署、策略、技能、知识和 MCP 工具集合变更时间线，以及按租户过滤的管理员/审计员查询接口。
- 成功判定统一为 `turn_outcome`；回合元数据默认保留90天，部署版本必须通过 BUILD_SHA 或本地启动器注入。见 ADR 0027。

## 未发布 — WP1 资源串行化与重叠授权

- 迁移 0006 为执行动作增加资源键和部分唯一索引，不同审批对同一资源的写入串行。
- 临时授权从有效台账继承原始权限；覆盖中的新授权独立安排回收，到期及 saga 按剩余台账恢复权限，避免提前收回或遗留临时权限。
- 审批卡展示同资源其他在途申请；跨会话申请保留给审批人判断。见 ADR 0026。


## 未发布 — 多租户阶段 3：租户策略、租户目标系统与平台管理员

见 ADR 0025。

- 租户策略覆盖 `config/policies/<tenant>.yaml`：只能在平台基线上收紧（审批人数、时长、可申请的系统和角色、审批人和代申请角色、需申请人确认的操作），放宽或基线没有的内容在加载时拒绝；没有覆盖的租户策略和版本号都与之前相同。示例见 `config/policies/acme.yaml.sample`。
- 审批、确认、复核、执行前检查、授权到期检查、智能体工具预检、事项回合、目标核验，都使用所属租户的策略；`revalidate(tenant)` 按租户复核；worker 发现任一覆盖变化时复核，并在该租户名下审计 `policy_loaded`。`GET /policy` 返回调用者所在租户的策略。
- 租户自己的目标系统 `config/tenant_adapters.yaml`（凭据只写环境变量名）：多租户模式下，部署自己的 Gitea 只为运营方租户执行，没有配置的租户只有本地沙箱。动作按审批单所属的租户选择适配器。
- 新角色 `platform_admin`（多租户模式下只在运营方租户 `IT_COPILOT_PLATFORM_TENANT` 内有效）：`/skills/reload`、`/ops/retention/run` 需要该角色；`/policy/revalidate` 由租户管理员发起时只复核本租户。单租户模式下行为不变。
- 静态守卫只认真正的租户过滤（`.c.tenant ==`、与 `principal.tenant` 的比较、写入时设置 tenant），只是读取 `row["tenant"]` 不算。

## 未发布 — 多租户阶段 2：按租户隔离的知识库

见 ADR 0024。

- 每个租户一个知识库（`KnowledgeLibrary`）：自己的向量 collection 和自己的 BM25 索引；`default` 沿用原来的 collection。检索、列出、导入、删除都只作用于调用者的租户；`knowledge_search` 的租户取自请求作用域，工具参数里没有 tenant 字段。
- `knowledge_search` 的结果缓存 key 包含租户，相同查询不会跨租户命中缓存。
- `/knowledge/stats` 改为需要登录（行为变化）。
- 跨租户巡检不再有已知缺口；知识库同时验证了两个方向。
- 未实现：平台公共知识库与租户订阅。多租户模式下 `/mcp` 的检索直接报错（它没有鉴权，不知道调用者的租户）。

## 未发布 — 多租户阶段 1：租户作用域与行级安全

见 ADR 0023。

- 租户作用域（`core/tenant_context.py`）：请求进入调用者的租户（`current_principal` 改为 async），飞书消息、执行 worker 的每个动作、事项 worker、目标核验进入各自的租户，启动过程与其余后台循环进入系统作用域 `*`。
- 多租户模式下：令牌必须带 tenant；签发方必须是租户登记的 issuer（没有登记时用默认签发方），不匹配返回 401；OIDC 也信任各租户登记的 realm，未知签发方在取公钥之前就拒绝；飞书消息必须来自租户登记的工作区。
- 迁移 `0004`：`actions`、`access_grants`、`audit`、`approval_decisions` 增加 tenant 列；沙箱目录 `directory_*` 的主键加上 tenant，每个租户有自己的目录（`seed_directory(tenant)`）。`/business/employees/{id}` 只查本租户，跨租户泄漏已修复。
- 迁移 `0005`：PostgreSQL 行级安全。每个事务设置 `app.tenant`，未设置时什么都看不到。多租户模式下，连接角色能绕过 RLS（超级用户或 BYPASSRLS）时拒绝启动；`deploy/postgres/app-role.sql` 创建普通角色 `itc_app`。默认 compose 暂不切换角色。
- worker 在多租户模式下不领取停用租户的动作，恢复后按原顺序继续执行。
- `WorkflowStore.employee(employee_id, tenant)` 必须传 tenant；测试中直接操作沙箱目录时用 `tests.conftest.run_in_tenant`。

## 未发布 — 多租户阶段 0：租户注册表与隔离巡检

见 ADR 0022 与 [docs/multi-tenant-plan.md](docs/multi-tenant-plan.md)。

- 租户注册表 `tenants`（迁移 `0003`，登记 `default` 与已有数据的租户）。开关 `IT_COPILOT_MULTI_TENANT` 打开后，令牌里的租户必须已登记且为 active：未登记返回 401，停用返回 403，查询失败返回 503；飞书消息对停用租户直接回复说明。开关打开时拒绝使用 SQLite。默认关闭，行为不变。
- `scripts/tenants.py`：开通、停用、恢复、下线租户，变更与管理审计在同一事务中写入。
- `tests/test_tenant_isolation.py`：另一租户的四种身份逐个调用所有路由，每条路由都必须归类；已知缺口（知识库列出、检索、删除，沙箱目录查询）按严格 xfail 运行。
- `tests/test_tenant_query_guard.py`：对带 tenant 列的表做查询却没有租户条件的函数，必须列入白名单并写明理由。

## 未发布 — 分层复盘：申请人确认与审计完整性

由意图识别 → 工具授权 → 人工确认 → 审计 → 回滚的分层复盘驱动（ADR 0020、0021）。

- 申请人确认（ADR 0020）：策略 `requester_confirmation` 中的操作（权限、重置、解锁、锁定）先生成只有申请人可见的草稿（审批状态 `draft`），申请人在卡片上点“确认提交”（`POST /approvals/{id}/confirm`，绑定哈希）或在对话里回复“确认”后才进入审批人队列；回复“取消”放弃。确认由代码处理，不调用模型。草稿默认 24 小时过期（`confirmation_ttl_hours`），审批人对草稿投票返回 409。新字段 `ChatResponse.awaiting_confirmation`、SSE 事件 `confirmation_required`。飞书、共享事项同样适用。
- 策略按内容识别（ADR 0021）：生效版本为 `声明版本号+规则指纹`，只改规则不升版本号也会复核在途工作；worker 首次加载新版本时写全局审计 `policy_loaded`。`approvals.policy_version` 加宽到 80。
- 来源摘要：审批 `created` 审计记录意图、知识引用和本轮工具原始参数（脱敏），不进哈希。
- 管理审计：先写 `started` 再写 `succeeded`/`failed`，崩溃留下 `interrupted`；`GET /admin/audit?outcome=failed|interrupted`。`audit` 与 `admin_audit` 由触发器拒绝 UPDATE/DELETE（迁移 `0002`）。
- 写操作清单统一到 `workflow/actions.py`，工具注册、审批识别、策略必填参数、意图问题与补偿判断都从这里推导；不一致时进程拒绝启动。

## 未发布 — 回答与业务状态一致性（深度评测驱动）

由全流程深度评测（[docs/evaluation-deep.md](docs/evaluation-deep.md)）发现的问题驱动。

- 声称核对：`agents/prompting.py::claim_problems` 取代单条正则 `unbacked_write_claim`。按句识别“已提交/已生成/已撤回”类声明（覆盖“已按你的要求提交”“请求已生成”等语序），否定/计划只看所在分句，排除复述用户自己的行为；按类别（工单/权限/重置/解锁/锁定）对照本轮成功的写工具；写工具失败而回答未说明时要求如实说明原因；Agent 没有撤回工具，只有本会话确实建过审批时才核对“已撤回/不再推进”。意图识别判定本轮未要求写操作时不核对，避免把复述过去纠正成“当时没提交”。
- 纠正提示带上具体不一致之处，并要求不提“核对/更正”；纠正一次后仍不一致，回答末尾追加确定性的“【以审批记录为准】…”说明。新指标 `it_copilot_agent_claim_checks_total{outcome=corrected|noted}`。
- 回答输出前：去掉模型照抄历史格式伪造的 `[本轮工具：…已生成待审批 …]` 执行记录；用户本轮贴出的密码/验证码替换为 `******`。
- 纯英文消息在本轮提示里明确要求英文回复；规则补充“工具失败如实说明原因”“用用户的语言回复”。
- 思考档位：新增 `IT_COPILOT_AGENT_THINKING`（disabled/low/high/max，只作用于 Agent），默认 disabled。实测 4 档 × 160 用例：开思考无可测量收益，成本 1.5–3.5 倍、首字时延翻倍（docs/evaluation-deep.md §10）。
- Token：规则加“不写过渡语”、回复“每个问题一般不超过 200 字”（system 预算 300→320 字）；预取知识改为紧凑文本（`knowledge_text`）；情景记忆加余弦距离门槛（`IT_COPILOT_MEMORY_MAX_DISTANCE`，默认 0.6），改按相关度 × 重要度排序；技能支持 full/progressive/auto 披露（`IT_COPILOT_SKILL_DISCLOSURE`，SKILL.md 新增 `intent_groups`、`write_tools`），当前规模下 auto 取 full。同为关闭思考时，总输入 −5% ~ −10%、输出 −10% ~ −13%、时延 p50 −6% ~ −10%。
- 用户画像改为四个字段的短文档（背景、工作内容、设备与环境、沟通偏好，每项一句话、限 80 字，schema 4），像 AGENTS.md 一样每次在旧版本上整份修订；提炼只看员工自己说的话，不写故障、工单等一次性事务。删除事实列表、`stable_attributes` 与 `FACT` 状态机；schema 3 画像读取时就地转换。长会话里画像从平均 2,654 字降到 42 字。
- 长对话：会话摘要原样保留错误码、事件 ID 等标识；压缩推迟时历史按 10 条一块截断，不逐轮滑动以保住前缀缓存。新增 `scripts/evaluate_long_dialogue.py`（4 个会话共 84 轮，回忆探针与写操作终态），`evaluation/application.py` 加 `maintenance` 开关。84 轮总费用 −45%（画像提炼 −88%），探针 19/19 与写操作 4/4 持平（docs/evaluation-deep.md §11）。
- 评测：`evaluation/token_audit.py` 按组成部分统计每次模型调用并记录真实用量与费用，三个业务评测脚本默认开启；`scripts/compare_token_modes.py` 对比多组报告。
- 评测：Jev 预取命中计为知识检索证据（`evaluate_application.py`、`evaluate_simulated.py`）；`forbid_text` 前 4 字内有否定词时不计；新增 `scripts/evaluate_experience.py`（真实口吻对话 + SSE 时延）与 `scripts/evaluate_rag_gitlab.py`（GitLab 公开 Handbook 检索）。
- 前端：全部改为 TypeScript（strict，CI 增加 `npm run typecheck`）；App.vue 拆成页面（`src/views/`）、接口层（`src/api/`）与可单测的会话模块（`chatSession`、`authSession`），对话切页不丢；共享事项轮询改为单飞，临时失败不再关闭事项；OIDC 配置缺 `issuer`/`client_id` 时直接报错；构建依赖移到 devDependencies。
- 评测：新增冻结的意图盲测集 `evaluation/datasets/intent_v4_blind.json`（83 条，12 个维度，只运行、不用于调优）。生产路径宽松 83/83、严格 73/83（88.0%），只用 Jev 时严格 70/83；v3 的 test 划分参与过 prompt 调优，README 不再称其为留出集（docs/evaluation-deep.md §12）。
- 审批：新申请只覆盖旧单中的部分动作时，也替代旧单，未覆盖的动作另存为一张“余下部分”的申请（沿用原冻结参数、状态与期限，审批人数重新计算，已投的票不带过去；审计记录 `remainder` / `carried_from`；回复说明保留了哪些事项）。修复一轮捆绑多个写操作、之后补充其中一个时产生的重复工单和重复重置（ADR 0007 修订）。多轮评测重跑：重复执行 7 → 0，T3 pass^3 0.50–0.67 → 0.58–0.75（p = 0.14，不显著）。
- 评测：`scripts/evaluate_simulated.py --user-model` 可为模拟用户指定不同的模型，报告记录 `user_model`。多轮评测按 2 种模拟用户 × 3 遍重跑：pass^1 0.87，pass^3 0.74–0.76，T3 pass^3 0.50–0.67（docs/evaluation-deep.md §13）。
- 修复评分：`lookup_asset` 不带 `asset_id` 时列出本人设备，目标设备属于本人时算命中（`evaluation/simulated.py::_OWN_LISTING`）；之前把 39 次正确调用判为失败。
- 文档：README 首屏改为一句话定位 + 关键结果；新增 `docs/iteration-stories.md`，整理 6 次评测驱动的迭代。
- 修复：临时失败后重新排队的动作在重试时不再检查策略，策略收紧后仍会开通权限；现在每次执行尚未生效的动作前都复核策略（ADR 0008）。
- 修复：审批人令牌缺少 `employee_id` 时跳过“不能审批本人账号”的检查；现在一律拒绝审批。

## v4.0 — 精简（破坏性更新）

不兼容旧数据库与旧数据，升级需重建数据库（本地删除 `data/it_copilot.sqlite3`，部署环境重建 PostgreSQL 库）。

- 迁移：14 个历史迁移合并为单一基线 `0001_baseline_schema`。
- 评测报告移出 git（`evaluation/reports/` 加入 `.gitignore`，由脚本本地生成）；删除只读报告的 `GET /eval/reports` 与前端“离线评测”面板，运维页改名“运行状态”。
- 删除 v1 离线冒烟门禁（`evaluate_all.py` 及其意图/记忆/RAG/Planner/phase6 套件）和 v2 时期的消融、重排、恢复、Judge 脚本，以及只被它们使用的数据集（`intent_v1`、`intent_v2`、`rag_v1`、`rag_v2_queries`）；意图评测默认使用 `intent_v3`。
- 删除旧记忆归属迁移（`memory/scope_migration.py`、`scripts/migrate_memory_scope.py`、`memory_scope_bindings` 表）、知识库旧版文档 ID 与旧演示文档哈希的兼容逻辑、`run_local.sh`（并入 `start_local.sh`）。
- 只保留一个 Agent：删除多 Agent 规划（`agents/planner.py`、bounded / bounded_all / single 三种模式、5 个专职 Agent、`AgentType`、性能路由、降级冷却与 Triage 兜底、`PLAN_RUN` / `HEALTH` 状态机）、`IT_COPILOT_PLANNING_MODE` 等配置、技能与 MCP 配置里的 `agents` 字段、`scripts/compare_strategies.py` 与 ADR 0006。`ChatResponse` 去掉 `agent_type`、`routing_confidence`、`plan`、`findings`；`/monitor` 的 `agents` 改为单个 `agent`；Agent 相关 Prometheus 指标去掉 `agent` 标签。评测脚本不再有 `--planning` / `--strategies` 参数。
- 删除技能自改进循环（`evaluation/improvement.py`、`scripts/self_improve.py`、ADR 0011）。
- 检索增强拆成两个独立开关：`IT_COPILOT_RAG_RERANK`（召回 20 个候选后重排）与 `IT_COPILOT_RAG_REWRITE`（查询改写），均默认关闭；`search_with_rewrite` 改名 `enhanced_search`。
- 只读工具：`lookup_asset`、`query_ticket` 改为按登录身份读本人的沙箱设备/工单台账（原先返回固定假数据）；删除没有数据源的 `check_service_status` 和硬编码 4 个错误码的 `lookup_error_code`，错误码说明移入知识库 `knowledge/it_helpdesk/common-error-codes.md`。评测数据集相应改为以知识检索作为错误码证据，`simulated_v4` 去掉“服务状态”原子后按原种子重新生成，设备资产号改为员工本人的 `DEV-<工号>`。
- 删除无调用方的 `LLMJudge` 与 `evaluation/task_browser_app.py`。
- 删除工具输出注入检测（`agents/injection_guard.py`、`IT_COPILOT_INJECTION_GUARD`、`AgentToolSpec.untrusted_output`、trace 的 `injection_score`、`scripts/evaluate_injection.py`、`scripts/build_injection_dataset.py` 与 `injection_v1` 数据集）。写操作仍由身份、策略与人工审批拦截。
- 长任务故障评测的崩溃点改为“Agent 调用已持久化、进度建议生成期间”。
- 文档：`docs/evaluation-v3.md` 改为 `docs/evaluation.md`，删除 `docs/evaluation-v2.md` 与已废弃的 ADR 0010，README 与 ADR 去掉迁移编号和升级说明。

## v3 — 可信身份、可恢复执行与实时事件流

- 身份：请求身份只取自已验证 JWT（OIDC/Keycloak 或开发令牌），请求体身份字段被忽略；RBAC 四角色；trace、审批、审计、员工状态、事件流按归属授权；`client_request_id` 按调用者隔离，修复不同用户同 ID 时 trace 互相覆盖、审批冲突的问题。移除共享管理员令牌。
- 策略与审批：`config/access_policy.yaml` 版本化策略（可申请的系统/角色、审批人数、最长有效期、代申请角色），工具与审批库双重校验；职责分离；developer 权限需两名审批人，投票做版本号 CAS。
- 执行：新增 `workflow/`（SQLAlchemy + Alembic，SQLite/PostgreSQL）与 `adapters/`（Gitea、本地目录）；审批通过同事务写入 outbox，租约 worker 执行；确定失败/可重试/结果未知三分类，结果未知先回查；baseline 先持久化；到期自动回收并做漂移检查；批量失败 saga 补偿；管理员对账接口。替换 `tooling/business_store.py`。
- 实时事件流：执行事件写入 Redis Streams，SSE 实时推送、`Last-Event-ID` 续传，执行与连接解耦。
- 统一工具执行层：所有 Agent 工具经超时、熔断、只读重试与写操作幂等键。
- 检索：向量召回在部门过滤后命中不足时自适应扩窗，修复其他部门文档挤占候选导致返回为空的问题。
- 部署：compose 增加 postgres / keycloak / gitea（含初始化任务）/ 独立 worker；修复镜像构建时 `data/` 目录权限导致构建失败；前端 OIDC 登录（PKCE）、演示身份切换、审批页、实时事件展示。
- 质量：GitHub Actions（lint、离线测试、PostgreSQL、Keycloak+Gitea 集成、前端）；21 项安全回归、真实 TCP 流式回归；后端 183 项测试（另 3 项真实服务集成测试按需运行），前端 18 项。见 docs/evaluation.md、docs/adr/、docs/threat-model.md。
- 复合诊断建单：计划 token 预算默认 30000（`IT_COPILOT_PLAN_TOKEN_BUDGET`）；Endpoint/Network/Access 开放仅生成审批的 `create_ticket`；模型轮数默认 5（`IT_COPILOT_MAX_MODEL_ROUNDS`）；计划含工单步骤时诊断步骤隐藏建单工具。
- 新增 `generalist` 对照策略与 64 场景分层评测集（`application_strata_v3.json`）、按领域证据与多效果检查、`compare_strategies.py`（Wilson 区间、配对 McNemar）；结果：single 40/64、bounded 44/64、generalist 63/64。
- 真实模型（deepseek-flash）重跑应用评测：开发集 24/24、23/24，留出集 36/36、36/36（v2：23/24、22/24、36/36、33/36）；修复评测脚本工号解析缺陷，报告记录实际上游模型。

## v3.1 — 多 Agent 修复与多轮状态评测

- 多 Agent 规划：新增 LLM 规划器（结构化输出，失败回退规则规划器）；只在用户要求时追加建单步骤；token 预算按步骤数计算（每步 10000）。在第 6 节同一批 42 个场景上，多 Agent 从 26/42 升到 41/42。
- 新增 `bounded_all` 策略（专职 Agent 持有全部工具）；默认策略改为 `generalist`。
- 审批：同一会话、同一申请人的新申请如果完全覆盖某张仍待审批的旧单，旧单标记为 `superseded`，不能再被批准，替代关系写入审计。修复多轮对话中重复建单（修复前占多轮评测失败的 72%）。
- 新增 τ-bench 式多轮评测（`evaluation/simulated.py`、`scripts/evaluate_simulated.py`、`simulated_v4.json`）：组合任务、LLM 模拟用户、按最终状态评分、pass^k、模拟用户越界标记；有测试检查任务可解、“什么都不做”判为失败。
- `compare_strategies.py` 支持按策略、按轮次拆开的报告文件，新增 `--exclude-simulator-flags`、`--exclude-errored`。
- 评测运行后段模型服务额度耗尽，部分分层没有有效数据，详见 docs/evaluation.md 第 7 节。
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

## v3.10 — 默认 embedding 换为中英双语模型，HTTP 客户端统一

- 默认 embedding 由 `BAAI/bge-small-zh-v1.5` 改为 `jinaai/jina-embeddings-v2-base-zh`（Apache-2.0，约 640MB）。WixQA Expert 向量 R@5 0.29 → 0.57、混合 0.41 → 0.57；rag-v2 混合 Recall@3 / MRR 0.975 / 0.900 → 1.0 / 0.958；记忆评测不变；查询延迟约 10 → 20 ms。已有数据在首次打开集合时按新模型重新向量化。还比较了 multilingual-mpnet、embeddinggemma-300m、Qwen3-Embedding-0.6B-Q，见 docs/evaluation-public.md。
- `core/embedding.py` 支持按模型配置查询与文档两侧前缀（e5、embeddinggemma、Qwen3）；`scripts/evaluate_rag_ablation.py` 新增 `--embedding-model`，非默认模型的报告按模型名分开存；原 bge-small-zh 报告（含在线改写重排）改名为 `rag_ablation_v2_bge-small-zh-v1.5.json`。
- HTTP 客户端统一为 `httpx2`：mcp、openai、pydantic-ai 都依赖它，项目代码（Gitea 适配器、飞书、告警 Webhook、评测与脚本）从 `httpx` 迁移过来，requirements 去掉 `httpx`。

## v3.11 — Jev（TypeSafe System One）接入与难例评测

- 意图难例集 `evaluation/datasets/intent_v3.json`：156 条，覆盖口语、易混、否定、多诉求、多轮、改口、伪装范围外、注入、日志、信息不足等 13 个维度，附 12 条标注规则与可接受意图；评测器新增严格准确率、分维度/分划分准确率、紧急度与延迟统计。
- 修复追问继承覆盖 LLM 结果：“还是不行”会把 LLM 已识别的 vpn_issue 改成组代表 network_issue；分类 prompt 补上与标注规则一致的类别边界。DeepSeek 在 v3 test 划分 71–72/75 → 75/75。
- 意图识别默认 Jev 优先（`IT_INTENT_BACKEND=auto`，有 `TYPESAFE_API_KEY` 时生效）：置信度 < 0.7、超时（2s）或失败时转 LLM；系统与角色按策略枚举做选择题，工号、工单号由正则抽取（新增裸编号与历史回读）。v3 上 155/156、test 75/75、严格准确率 91.0% → 97.4%，约 6% 请求转 LLM，p50 1.30s → 0.30s；补槽结果与纯 LLM 一致。Jev 置信度校准误差 0.045（DeepSeek 0.100），DeepSeek 的自报置信度区分不了对错。
- 结果重排新增 Jev 后端（`IT_COPILOT_RERANK_BACKEND=jev`），默认仍为 LLM：WixQA Expert MRR 不重排 0.50 / Jev 0.56 / LLM 0.69（Simulated 0.36 / 0.46 / 0.64），Jev p50 0.3s、LLM 1.1s。见 `rerank_wixqa.json`。
- 只读工具返回的提示注入检测（`agents/injection_guard.py`，`IT_COPILOT_INJECTION_GUARD=jev`，默认关闭）：353 条检测集（AgentDojo 5 种攻击模板 + 本项目中文 SOP 困难负例与中文注入）上 Jev 与 DeepSeek 均 100% 检出、0 误报，Jev p50 0.29s。AgentDojo travel 只读攻击 20 例端到端未见收益：开关前后模型都没有真正替攻击者推荐，计为“成功”的都是模型在警示里点名了被推广的酒店。
- 新增 `core/jev.py`、`scripts/evaluate_intent_jev.py`、`scripts/evaluate_rerank.py`、`scripts/build_injection_dataset.py`、`scripts/evaluate_injection.py`；`evaluate_agentdojo.py` 新增 `--injection-tasks`、`--report-suffix`。测试 261 项通过。

## v3.12 — 上下文预算、前缀缓存、实时正文流与模块解耦

- 上下文：知识检索结果只给模型标题、正文、日期和短引用（5 条结果 3076 → 802 字符，去掉分数与 64 位哈希，模型也不再把长哈希抄进回答），`top_k` 上限 20 → 10；最近对话按真实多轮消息传入，助手回答截断到 600 字；回答要求“先结论、一般不超过 300 字”。
- 前缀缓存：system prompt 只放每个 Agent 固定的内容，skill、缺失字段、依赖证据、背景与实体移到本轮用户消息；历史截断是确定的，相邻轮次前缀一致。token 用量新增 `cache_read_tokens`，指标 `it_copilot_llm_tokens_total{direction="cache_read"}`。
- 实时正文：单 Agent 路径边生成边推送 `answer_delta`，转去调用工具时推送 `answer_reset`；`content_delta` 语义不变。前端渲染回答草稿。
- 应用缓存命中计数 `it_copilot_cache_lookups_total{cache,result}`（工具结果、意图）。
- 解耦：`agent_orchestrator.py`（782 行）拆为 `agents/types.py`、`agents/base_agent.py`、`agents/agent_orchestrator.py`；`AgentToolSpec`/`make_tool` 移到 `tooling/tool_spec.py`（tooling 不再依赖 agents）；`workflow/db.py` 移到 `core/db.py`（core 不再依赖 workflow）；知识检索标为 `self_governed`，只由 MCPToolManager 负责超时/熔断/缓存，执行层不再叠一层；意图类别规则合并为一张表，删除评测专用的 `evaluation/jev_intent.py`。
- 修复评测脚本把 `token_usage` 所有字段相加：现在只计输入 + 输出（`evaluation.reporting.billable_tokens`）。
- 验证（DeepSeek，意图走 LLM 以便与基线对比）：应用分层集 64 场景 64/64 → 64/64，端到端延迟 p50 6.64 → 4.97 s，Agent 输入 token −29%、输出 −45%，输入的 84% 命中前缀缓存（`application_strata_v3_context_full.json`）；多轮模拟用户 50 任务 33/50、27/50 → 41/50（10 个任务从两次都失败变为通过，1 个从两次都通过变为失败），中位延迟 28.4 → 25.2 s。意图 v3 留出集仍为 75/75。测试 265 项通过。

## v3.13 — 精简提示词与工具、记忆去重、bug 修复与测试补全

- 提示词（pi 风格）：每个 Agent 的 system prompt 改为“一句角色 + 共用规则”，去掉与工具定义重复的工具白名单和角色/职责/流程/输出契约；`AgentProfile` 只留 temperature 与 max_tokens。规则每条对应评测里出现过的失败：先查 SOP 并写 citation、用户要求引用时本轮重新检索、只在用户明确要求时建单或提交申请。
- Skill：每个 Agent 适用的 skill 全部放进 system prompt（可信位置、对每个 Agent 固定、前缀缓存稳定），不再按关键词注入到用户消息里；声明与规则冲突时以规则为准。access skill 写明申请字段以工具参数为准。
- 工具：移除 `inspect_request_context`、`suggest_required_fields`、`build_diagnostic_plan`（信息已在提示里或结果恒定，74 轮评测里被调用 33 次，每次多一轮模型往返）；接待 Agent 不再需要工具；精简工具描述。
- 记忆：情景记忆只检索其他会话（当前会话已在多轮历史与摘要里，原实现会重复注入）；多轮历史取整个工作记忆（原先最近 8 条与压缩阈值 15 条之间的消息哪里都看不到）；助手历史附一行本轮工具/审批记录（只有回答文字时模型会在后续轮次否认自己做过的事）；画像提炼约 3 轮一次、成功才推进计数；画像 JSON 不再转义中文。向量过滤新增 `$ne`。
- Bug：裸单号/工号正则把 `incident`、`including` 当单号、把设备错误码 `E1003` 当工号；计划预算把缓存命中 token 重复计入；对话评测把历史同时作为消息和背景发两遍；`self_governed` 工具丢了总时限；注入检测扫描本地工具且每个 Agent 各建一个连接池（改为 `untrusted_output` 标记 + 编排器共享、关停时释放）；Jev 低置信度后 LLM 也失败时丢弃 Jev 答案；数值环境变量为空字符串时进程起不来（统一 `core/env.py`）；空白检索词不回退用户原话；`BaseAgent` 不传 profile 时报错；登录员工工号不计入槽位导致建单被拦（身份敏感的重置/解锁仍需用户明确给出）；模型声称已提交但本轮没有写操作成功（检测后纠正一次）。
- 测试：后端 265 → 312 项（Jev 客户端、流式事件、注入包裹、两轮 `/chat` 历史、记忆去重与节流、工具 Schema 与体积预算、意图规则与编号边界、无据声称纠正等），前端 14 → 22 项（回答草稿逻辑抽成 `lib/answerDraft.js`）。
- 验证（DeepSeek，意图走 LLM 以便与基线对比）：应用分层集 64/64 → 64/64，Agent 输入 token 467k → 296k（−37%）、输出 49k → 20k（−59%），输入 86% 命中前缀缓存，工具调用 219 → 139，端到端延迟 p50 6.64 → 4.75 s、p95 12.67 → 8.79 s（`application_strata_v3_final.json`）；多轮模拟 50 任务 33/50、27/50 → 41/50（排除模拟用户越界后 35/40），Agent token 1.54M → 0.79M，中位延迟 28.4 → 19.4 s（`simulated_v4_generalist_final.json`）。单次运行有波动：c2-asset-grant-jira 在多次重跑中偶有失败。

## v3.14 — Jev 前置判断、精简 skill、模块拆分与难例应用评测集

- Jev 在意图识别的同一请求里并行回答两件事：是否需要知识库（需要时把与意图识别并行预取的检索结果附进本轮消息，省掉 Agent 专门检索的一轮；预取出错按无结果处理）、用户这一轮要求了哪些写操作（没要求的写操作在执行前拒绝，工具列表不变以保持前缀缓存）。没有 Jev 时两项都不生效。
- Skill 精简约一半（去掉与规则、工具描述重复的内容；删掉“建单后必须返回 ticket_id”这条与审批流程矛盾、导致模型反复改口的要求），skill 块不再带 description。
- 拆分：`agents/intent/taxonomy.py`（类别、规则表、Jev 问题）、`agents/intent/entities.py`（编号抽取）、`agents/prompting.py`（规则、本轮消息、历史、无据声称识别）、`agents/agent_stats.py`；登录工号默认逻辑移到 `conversation_state.with_login_employee`。
- 清理：删除只被测试使用的 `EndToEndEvaluator` 及其数据结构（evaluator.py 808 → 397 行）、`call(rerank_top_k=…)` 死路径、`Skill.matches`、未被引用的 `safety_v1.json`、运维面板里展示已删除融合策略的 intent_v2 套件。
- Bug：资产号 `DEV-023` 被当成错误码；“错误码是 E1003”被当成工号；用户本轮要求重新提交时，“历史里提交过”的豁免让无据声称漏过纠正；评测器 `no_approval` 只看最后一轮、写操作结果只认最后一轮的审批。
- 新增难例应用评测集 `application_hard_v4.json`（46 场景：长对话、不该写的陷阱、难措辞的写操作、带干扰文档的引用、安全、多个写操作）及检查项 `forbid_text`、`require_text_any`、`distractors`、`forbid_grant`、多权限、`free`。同一套题：改动前代码 43/46（错用历史背景文档作出处、回显用户粘贴的密码、长对话漏提交）、当前代码 + LLM 意图 44/46、当前代码 + Jev 46/46；p50 延迟 7.2 → 4.5 → 3.3 s，Agent token 549k → 271k → 270k。
- 原 64 场景集（Jev 意图）63/64；多轮模拟（Jev 意图）37/50，失败多为模拟用户提出任务外的写请求，与此前 41/50 的差异在两次运行的波动范围内。测试 353 项。

## v3.15 — 申请撤回、权限提前归还、知识库文档管理、飞书防重放、限流与数据保留

- 审批：新增终态 `withdrawn` 与 `POST /approvals/{id}/withdraw`，只有申请人本人可撤回仍待审批的申请；与投票共用版本号 CAS，撤回与批准并发时只有一方生效；状态、审计、飞书通知同事务提交，共享事项视为终态（ADR 0017）。
- 限时授权：`POST /approvals/{id}/release` 让申请人、权限持有人或 IT 管理员提前归还生效中的权限；只把授权时排好的回收动作提前，worker 仍做漂移检查，不会删除原有或被他人改动的权限。前端审批卡片新增“撤回申请”“提前归还权限”，审批页新增“已撤回”筛选。
- 知识库：管理员可 `GET /knowledge/documents` 按文档查看（部门、片段数、来源、是否演示种子），`DELETE /knowledge/documents/{id}` 删除过时文档，BM25 与向量两路同时移除、检索缓存立即失效；演示种子文档在非生产环境重启后会重新导入，响应与界面会提示。知识库页新增管理员文档列表。
- 飞书：启用签名时校验 `X-Lark-Request-Timestamp` 在 ±`FEISHU_MAX_CLOCK_SKEW_S`（默认 300 秒）内，拒绝事后重放；威胁模型对应条目由“未覆盖”改为有测试。
- 策略复核覆盖代他人申请：审批保存申请人提交时的角色快照（不进冻结哈希）；新策略不再允许以该角色代申请时，待审批失效、未执行动作取消、已生效授权立即回收。快照之前的旧审批不做推测（ADR 0008）。
- 限流：按租户＋用户的 GCRA 限流，`/chat`、`/chat/stream`、要求 Agent 回复的事项消息与飞书消息共用默认 30 次/60 秒的预算（`IT_COPILOT_RATE_LIMIT_AGENT_TURN`）；Redis Lua 原子执行、多进程共享，429 + `Retry-After`，飞书回复一句提示；Redis 故障时放行。评测框架关闭限流（ADR 0018）。
- 数据保留：每小时清理处理完的飞书收发记录（默认 30 天）与情景记忆（默认 90 天）；未结审批关联的消息保留到审批结束；审批、执行与审计记录不清理。管理员可 `POST /ops/retention/run`，指标 `it_copilot_retention_deleted_total`（ADR 0018）。
- 对话中的撤回与归还：用户提到撤回、归还、“我的申请”等时，服务端按身份召回本人可撤回的申请与可归还的权限，放进本轮上下文并通过 `related_approvals` 返回，网页显示卡片供本人点击；Agent 不获得撤回/归还工具（ADR 0017）。
- 管理操作审计：新增只追加的 `admin_audit` 表（迁移 0012），记录知识库导入/删除、策略复核、技能重载与数据清理的操作人和明细；`GET /admin/audit` 供管理员与审计员按租户查看、按 id 翻页。
- 令牌吊销：按身份的吊销水位线（`session_revocations`，迁移 0013），签发不晚于水位线的令牌一律 401；管理员 `POST /admin/sessions/revoke`（subject 或用户名）、本人 `POST /auth/logout-all`、`GET /admin/sessions/revocations`；查询失败拒绝请求，进程内缓存 5 秒（ADR 0019）。
- 设备远程锁定：新写操作 `lock_device`（Endpoint/通用 Agent），与其他写操作一样只生成待审批请求；本地沙箱设备台账（`directory_devices`，迁移 0014，每个演示员工一台 `DEV-<工号>`）；执行前核对设备属于申请中的员工；策略版本升为 2026-11-12.1。工具目录体积预算 2600 → 2800 字符。
- 测试：新增 `tests/test_requester_actions.py`（撤回、并发、归还、漂移、HTTP）、知识库列表/删除、飞书时间窗、代申请复核、限流（`tests/test_rate_limit.py`）、保留清理（`tests/test_retention.py`）、对话召回（`tests/test_request_recall.py`）、管理审计（`tests/test_admin_audit.py`）、令牌吊销（`tests/test_session_revocation.py`）、设备锁定（`tests/test_device_lock.py`）；前端新增权限与控制器用例。

