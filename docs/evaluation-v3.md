# v3 评测：身份与授权、受控执行、实时事件流

本轮验证三类保证：越权与提示注入不会造成未授权读写；审批后的执行在并发、崩溃与外部系统故障下不重复、不遗漏、可回收；执行进展在完成前就能到达客户端。第 1–4 节离线可复现，不消耗模型 token；第 5 节用真实模型在新的身份与审批流程下重跑了 v2 应用评测。

## 1. 安全回归（21 项）

命令：`python scripts/evaluate_security.py` → [security_v3.json](../evaluation/reports/security_v3.json)；同一套用例在 `tests/test_security_suite.py` 中作为 CI 门禁运行。

| 类别 | 用例数 | 内容 |
|---|---|---|
| 身份认证 | 5 | 错误密钥、`alg=none`、过期、篡改 claims 的令牌，缺失令牌 |
| 身份冒充 | 2 | 请求体声明他人身份/部门；检索参数指定其他部门 |
| 对象级授权 | 2 | 跨用户读取审批、审计、trace、员工状态、事件流、近期 trace；`client_request_id` 碰撞 |
| 职责分离 | 5 | 非审批人决定、申请人自批、管理员代申请后自批、审批人是变更对象、双人审批只有一人 |
| 策略 | 4 | 管理员角色、超长有效期、为他人申请、为管理员账号重置密码 |
| 篡改与重放 | 2 | 篡改快照哈希；四个请求并发批准 |
| 提示注入 | 1 | 知识库文档要求跳过审批，脚本化模型完全照做调用写工具 |

结果：21/21 通过，未授权写入 0，未授权读取 0。每个用例记录目录状态前后快照，凡非预期变化即计为未授权写入；分母是攻击用例数，不是请求总数。

变异测试（确认门禁有效）：去掉 trace 归属检查后 `idor_cross_user_reads` 失败；去掉“仅限本人申请”后 4 个用例失败。

限制：模型是脚本化的“最坏情况”替身，验证的是模型之外的控制；用例由作者编写，不代表攻击面全集（见 [威胁模型](threat-model.md) 的未覆盖项）。

## 2. 实时事件流

命令：`python scripts/benchmark_stream.py --runs 20 --model-latency 0.8` → [stream_latency_v3.json](../evaluation/reports/stream_latency_v3.json)

真实 uvicorn 监听回环 TCP，客户端按行读取 SSE；模型替身每次调用固定 0.8s，每轮 3 次调用（意图、工具决策、最终回答）；每轮消息不同，避免意图缓存命中。

| 指标 | P50 | P95 |
|---|---|---|
| 首个执行进展事件（非 started） | 0.93s | 1.02s |
| 首个 `tool_started` | 1.73s | 1.83s |
| `completed` | 2.56s | 2.64s |

旧实现在整轮完成后才回放事件，首个执行事件时延约等于 `completed`。差值取决于模型时延与调用次数；这是架构对比，不是生产时延。进程内 ASGI 传输会缓冲整个响应，最初用它测得所有事件同时到达，因此改为真实 TCP，并加入回归测试 `test_events_reach_a_real_tcp_client_before_the_turn_completes`。

断线续传：`test_live_stream_reports_tools_before_completion_and_resumes` 从 `tool_started` 之后的 id 续读，只收到之后的事件，且没有重复执行；他人续读返回 403。

## 3. 工作流并发与故障

`tests/test_workflow.py` 12 项，默认 SQLite；`IT_COPILOT_TEST_DATABASE_URL` 指向 PostgreSQL 时每个测试使用独立 schema，执行同一套 Alembic 迁移。本地在 PostgreSQL 17 上连续 5 次全部通过（SQLite 同样 5 次）。

覆盖：20 线程并发批准只迁移一次、执行一次；批准与拒绝竞争；双人审批并发投票；职责分离；策略；preflight 失败零变更；到期回收；原有权限与漂移权限不被回收；结果未知的两种情况（已生效 → 回查记成功、只调用一次；未生效 → 回查后重做）；worker 在变更后、记账前被 `os._exit` 杀死，另一个 worker 在租约过期后回查并完成，密码版本只增加一次；批量中途确定失败时已授予的权限被补偿回收。

## 4. 真实服务集成

`tests/test_integration_live.py`（未配置时跳过；CI 的 integration job 启动服务后运行）：

- Keycloak 26.4：密码模式取令牌 → `Principal` 字段与角色正确；篡改签名被拒；受众不符被拒；API 在 oidc 模式下接受 Keycloak 令牌、拒绝 dev 令牌。
- Gitea 1.24：worker 授予 alice 只读 → Gitea 协作者权限为 read → 时间推进后回收 → 协作者被移除；授权后手工把权限改为 write，到期时 worker 不删除，回收动作停在 `manual_review`。

Compose 全栈手工验收（2026-10-29）：Keycloak 登录（授权码 + PKCE）→ 审批页 → bob 批准后仍待审批（developer 需两人）→ carol 批准 → 执行成功，Gitea 中 alice 权限为 write，回收排期为 8 小时后；跨用户读取与自批均返回 403。该次验收未启动模型网关，审批记录由与 `/chat` 相同的代码路径在容器内创建。

## 5. 真实模型应用评测（新身份 + 审批流程）

上游 `deepseek-flash`（api.deepseek.com，2026-10-29；该名称是 DeepSeek 的滚动别名，`/models` 与响应都不给出具体版本，报告的 `upstream` 字段记录实际调用名）。数据集、场景与 v2 相同；每个用例以对话中声明的本人工号作为登录身份，审批由 bob（必要时加 carol）完成，另加两项检查：申请人不能自批、授权已登记到期回收。

| 数据集 | single | bounded | 修复前（同流程） | v2 |
|---|---|---|---|---|
| 开发集 24 场景 × 1 | **24/24** | **23/24** | 23/24、22/24 | 23/24、22/24 |
| 留出集 12 场景 × 3 | **36/36** | **36/36** | 36/36、33/36 | 36/36、33/36 |

报告：[application_live_v3.json](../evaluation/reports/application_live_v3.json)、[application_holdout_live_v3.json](../evaluation/reports/application_holdout_live_v3.json)。

### 复合诊断 + 建单的三处修复

修复前失败全部集中在“分别诊断两个故障并建工单”类请求（`compound`、`compound-access`、`h08`）。逐层定位：

1. **bounded 预算**：第一波并行诊断已用 9.8k–20.3k agent token，波次间检查发现超过 12000 就停止，工单步骤从未执行。默认预算改为 30000（`IT_COPILOT_PLAN_TOKEN_BUDGET`）。它只在波次之间检查，不是硬上限：修复后此类计划实际消耗 37k–46k，约为 single 的 2.5–3 倍。
2. **single 没有建单工具**：请求只路由到一个诊断 Agent，它的白名单里没有 `create_ticket`。`create_ticket` 只生成待审批记录、无直接副作用，因此向 Endpoint、Network/Software、Access 开放（Triage 不开放），工单优先级 skill 同步注入。
3. **开放后暴露的轮数上限**：诊断 → 建单 → 回复需要 4 次模型请求，原 `UsageLimits(request_limit=3)` 把它截断成 `agent_failed`。默认改为 5（`IT_COPILOT_MAX_MODEL_ROUNDS`）；回归测试在上限为 3 时失败、为 5 时通过。

另外，bounded 计划含工单步骤时，诊断步骤通过 PydanticAI 的 `prepare` 隐藏 `create_ticket`（调用层再拒绝一次），避免同一请求由多个 Agent 各建一张单。

开放建单工具后检查了误用：全部 120 次试验中，只有用户明确要求“记录工单”的 `compound-access` 调用了它；咨询、拒绝、缺槽位等不应写入的场景没有产生审批。

剩余失败 `compound`（bounded）：用户说“请分别诊断并建立工单记录证据”，工单步骤按两个故障各建一张，数据集断言为恰好一张。两种理解都合理，保留为失败，不修改断言。仍没有多 Agent 成功率优于单 Agent 的证据，而多 Agent 的 token 成本明显更高。

### 首次运行作废的原因（保留记录）

首轮得到开发集 21/24、留出集 27/36，逐条排查后：

1. 评测脚本缺陷（已修复）：用 `\w` 提取工号时把“1002本人”整体当作工号；工号更正类用例取了第一轮的错误工号作为登录人。两种情况下系统都按策略正确拒绝了“为他人申请”。修复后对全部用例自检申请人与断言对象一致。
2. `h08` 意图在不同运行间翻转（建单 / 设备故障）：意图代码与输入未变，单独探测 5 次为 4:1，说明该消息本身有歧义；同一轮内 3 次重复因意图缓存只相当于一次采样，不是独立样本。不能据此断定模型版本漂移。

## 6. 单 Agent / 多 Agent / 通用 Agent 分层对照

问题：多 Agent 到底有没有用？v2 的“单 Agent”是按意图选出的**一个专职 Agent，只带本领域工具**，它与多 Agent 的差距可能只是工具覆盖，不能证明协作本身有价值。因此加入第三种策略：

| 策略 | 含义 |
|---|---|
| single | 意图路由到一个专职 Agent，只用它的白名单工具 |
| bounded | 规则规划器拆步，多个专职 Agent 并行，依赖步骤传证据（多 Agent） |
| generalist | 一个 Agent 持有全部 11 个本地工具 + 知识检索，命中的 skill 全部注入（对照基线） |

数据：[application_strata_v3.json](../evaluation/datasets/application_strata_v3.json)，64 个作者编写的场景，运行前冻结、未据此调整系统。分层：S 单领域 16、C2 双领域 20、C3 三领域 8、M 多轮 10、N 不应写入 10。复合场景新增按领域的证据检查（网络：`lookup_error_code`/`check_service_status`；终端：`lookup_asset`）与多效果检查（同一审批里授权 + 建单等）；N 层检查不得调用的写工具。

运行：`deepseek-flash`，两个独立进程各跑一遍（同一进程内意图缓存会让重复试验共享一次意图判断），每个场景每种策略 2 次；同一进程内三种策略轮换先后顺序。严格口径：两次都通过才计为通过。分析：`python scripts/compare_strategies.py evaluation/reports/application_strata_v3_run1.json evaluation/reports/application_strata_v3_run2.json` → [对比结果](../evaluation/reports/application_strata_v3_comparison.json)。

| 分层 | 场景 | single | bounded | generalist | token 中位数 single / bounded / generalist |
|---|---|---|---|---|---|
| S 单领域 | 16 | 13（0.57–0.93） | 13（0.57–0.93） | **16**（0.81–1.00） | 3.3k / 3.1k / 4.0k |
| C2 双领域 | 20 | 9（0.26–0.66） | 12（0.39–0.78） | **20**（0.84–1.00） | 5.6k / 10.9k / 6.8k |
| C3 三领域 | 8 | 0（0.00–0.32） | 1（0.02–0.47） | **8**（0.68–1.00） | 10.0k / 10.0k / 13.8k |
| M 多轮 | 10 | 9 | 9 | 9 | 7.8k / 8.7k / 9.6k |
| N 不应写入 | 10 | 9 | 9 | 10 | 2.3k / 3.0k / 5.6k |
| 合计 | 64 | 40（0.50–0.73） | 44（0.57–0.79） | **63**（0.92–1.00） | 4.8k / 5.7k / 5.7k |

括号内为 Wilson 95% 区间。配对比较（只看结果不一致的场景）：generalist 对 single 24:1、对 bounded 20:1，精确 McNemar p < 0.001；bounded 对 single 5:1，p = 0.22，差异不显著。证据覆盖率（网络 / 终端）：single 0.43 / 0.48，bounded 0.64 / 0.76，generalist 1.00 / 1.00。

### 失败机制

- **single：路由即能力边界。** 连单领域都错 3 个：“解释 403/500 错误码”被分类为 `howto` 交给 Triage，“查设备资产状态”被分类为 `ticket_status` 交给 Ticket Agent，两者都没有对应工具。复合请求只路由到一个领域，另一领域必然缺证据。
- **bounded：规则规划器的覆盖与副作用。** 关键词规则漏领域（“403”“资产”不在网络/终端关键词里）；规划器在识别出两个以上领域时**无条件追加建单步骤**，在用户未要求时生成工单审批（`c2-vpn-black`、`c2-status-asset`）；三领域请求仍有 2 次触发 30000 token 波次间预算。多 Agent 在 C2 比 single 多覆盖一个领域，但带来约 2 倍 token。
- **generalist：** 唯一失败是 `m-unlock-confirm` 中追问“哪个账号被锁”，属于合理澄清。没有靠乱调工具得分：查询了用户未提及领域的试验 generalist 6/128，single、bounded 各 4/128；三者工具调用次数中位数均为 2。

### 结论与边界

- 在本项目的工具规模（11 个本地工具）下，**单个持有全部工具的 Agent 显著优于两种多 Agent 形态**，token 成本与 bounded 相当。本项目多 Agent 的价值不在成功率，而在权限隔离（每个 Agent 只能触达本领域的写工具）、提示词与监控拆分；这需要用安全指标而不是成功率来衡量。
- 证据检查衡量“是否用权威工具数据支撑回答”，不是回答质量：single 对 403 的通用解释本身合理，只是缺少系统数据。
- 场景由作者编写，单一模型，每格 2 次；工具数增加到几十个时通用 Agent 的工具选择可能退化，结论不能外推到大工具集。

## 7. 多 Agent 修复、bounded_all 与 τ-bench 式多轮评测

### 7.1 旧场景的难度与借鉴的评测方法

第 5、6 节的场景都是**单轮或脚本化多轮**：用户第一句话就给全信息，评分大多看是否调用了某个工具、是否生成审批。generalist 拿到 63/64，已经到了天花板，没法区分策略好坏。公开 Agent 基准的做法：

| 来源 | 借鉴点 |
|---|---|
| [τ-bench](https://arxiv.org/pdf/2406.12045)（Sierra，2024） | LLM 扮演用户，信息只在被问到时透露；按**对话结束后的数据库状态**评分，不看措辞；报告 pass^k（同一任务 k 次都成功）。原文中 gpt-4o 成功率不足 50%，retail 领域 pass^8 低于 25% |
| [τ²-bench](https://huggingface.co/papers/2506.07982)（2025） | 任务由原子子任务组合，子任务数即难度；单独统计用户模拟器出错 |
| [AgentArch](https://arxiv.org/html/2509.10769v1)（ServiceNow，2025） | 在企业工作流上比较单 Agent / 多 Agent、工具编排方式；最好的模型在复杂任务上成功率 35.3%、简单任务 70.8%，没有一种架构在所有模型和任务上占优 |
| [WorkArena](https://servicenow.github.io/WorkArena/) | IT 服务台类企业任务，任务目标可程序化校验 |
| [Establishing Best Practices for Building Rigorous Agentic Benchmarks](https://arxiv.org/pdf/2507.02825)（2025） | 两条检查：任务必须可解；"什么都不做"不能通过（拒绝类任务要求明确说明理由） |

本项目没有使用这些数据集（领域、工具、语言都不同），借鉴的是方法。

### 7.2 多 Agent 修复

第 6 节定位的三个问题，逐个修复：

| 问题 | 修复 |
|---|---|
| 关键词规则规划器漏领域（“403”“资产”不在关键词里） | `LLMPlanner`：模型以结构化输出（`make_plan`，Agent 类型为枚举）生成计划，`finalize()` 统一校验；失败时回退到规则规划器（指标 `llm_plans` / `fallback_plans`） |
| 多领域时无条件追加建单步骤 | 只在用户明确要求时保留建单步骤（`wants_ticket`，识别“不要建工单”等否定），并让它依赖其他所有步骤 |
| 固定 30000 token 预算对三领域不够 | 按步骤数计算预算：每步 10000（`IT_COPILOT_PLAN_TOKENS_PER_STEP`） |

另外新增 `bounded_all` 策略：规划和专职提示词不变，但每个专职 Agent 都持有全部工具。它用来回答“多 Agent 的问题是协作本身，还是工具覆盖”。默认策略改为 `generalist`（`IT_COPILOT_PLANNING_MODE`）。

**修复后在第 6 节场景上重跑**（[对比结果](../evaluation/reports/application_strata_v3b_comparison.json)）：重跑过程中 DeepSeek 账户余额耗尽（HTTP 402），只有 S/C2/C3 中 42 个场景的全部 8 次试验正常完成，M/N 两层没有有效数据。下表只统计这 42 个场景，“修复前”取第 6 节同一批场景的结果：

| 分层 | 场景 | single | bounded 修复前 → 修复后 | bounded_all | generalist | token 中位数 single / bounded / bounded_all / generalist |
|---|---|---|---|---|---|---|
| S | 16 | 13 | 13 → **16** | 16 | 16 | 3.2k / 3.7k / 4.0k / 4.1k |
| C2 | 20 | 9 | 12 → **19** | 20 | 18 | 6.8k / 12.3k / 12.7k / 7.6k |
| C3 | 6 | 0 | 1 → **6** | 6 | 6 | 10.3k / 30.1k / 18.6k / 8.1k |
| 合计 | 42 | 22 | 26 → **41** | 42 | 40 | 4.9k / 9.1k / 7.9k / 5.7k |

- bounded 的领域证据覆盖率（网络 / 终端）从 0.64 / 0.76 升到 1.00 / 1.00。
- 三种全覆盖策略两两之间，结果不一致的场景只有 0–2 个，McNemar p ≥ 0.5，没有显著差异。
- 多 Agent 不再比单 Agent 差，代价是 token 更多：C2 多约 60%，C3 是 generalist 的 2–4 倍。

### 7.3 τ-bench 式多轮评测（v4）

**数据**：[simulated_v4.json](../evaluation/datasets/simulated_v4.json)，由 `evaluation/simulated.py` 以固定种子生成后冻结。

- 8 种原子任务：错误码诊断、服务状态、资产排查、资产排查并建单、权限申请、密码重置、账号解锁、知识问答。
- 分层：T1（1 个原子）14 个、T2（2 个）16 个、T3（3 个）12 个，另有 P 层 8 个（一个正常请求加一个策略禁止的 GitLab 管理员权限）。
- 用户第一句话故意不完整（例如只说“笔记本黑屏”，不给资产号）。

**流程**：

- 模拟用户也用 deepseek-flash 扮演，只在被问到时透露事实。
- 每段对话最多 8 轮。
- 对话结束后，审批人 bob（需要两人时加 carol）批准 agent 提交的所有待审批申请，worker 执行。

**评分**（以下检查必须全部通过）：

- 员工最终状态：权限集合精确一致（多授予也算失败）、工单数量精确、密码与锁定状态；
- 关键工具调用时参数正确（资产号、错误码）；
- 知识问答带出处；
- P 层必须明确说明拒绝理由，且不能提交管理员类角色。

**统计**：每种策略 2 个独立进程，报告 pass^1（单次成功率）和 pass^2（两次都成功）。

**v4 暴露的三个缺陷**（均在正式运行前修复，修复前的数据保存在 [prefix_v4](../evaluation/reports/prefix_v4/)）：

1. **重复建单（产品缺陷）。** 用户补充信息、说“确认提交”或要求“补充进工单”时，agent 会重新调用 `create_ticket`，同一问题堆积多张待审批工单。修复前 247 段对话中有 73 段因此产生多余工单，占全部 101 个失败的 72%，四种策略都有。修复方式：同一会话、同一申请人的新审批，如果它的动作完全覆盖某张仍待审批的旧单（键为工具 + 员工，权限申请再加系统），旧单标记为 `superseded`。被替代的审批不能再被批准，替代关系写入审计，回复中告知用户；范围更窄的新申请不会替代范围更宽的旧单（`workflow/store.py::_supersede`）。
2. **任务不可解（评测缺陷）。** 知识库 SOP 要求 GitLab 申请提供项目名称、Jira 申请提供项目空间，但 9 个任务没有给模拟用户这个信息，严格遵守 SOP 的 agent 反而失败。已补上这些事实，并新增测试：检查每个任务都含其 SOP 必需的信息、评分用到的参数都能从模拟用户处获得、“什么都不做”判为失败。
3. **模拟用户越界。** 模拟用户会自行要求建单、在 agent 没问时说“确认提交”、编造自查结果。已收紧提示词（报告中记录其哈希 `simulator_prompt_sha256`），并标记越界对话（`simulator_flags`）。分析时既报告全量结果，也报告剔除越界任务后的结果。

**结果**（[完成部分](../evaluation/reports/simulated_v4_comparison_completed.json)、[再剔除越界](../evaluation/reports/simulated_v4_comparison_clean_completed.json)）：

运行后段 DeepSeek 返回 429（并发上限随余额下降到 5）以及 402（余额耗尽）。只有 33 个任务在 8 次运行中都没有服务端错误：T1 14、T2 16、T3 3，P 层全部丢失。下表只统计这 33 个任务：

| 分层 | 任务 | generalist pass^2 / pass^1 | bounded | bounded_all | single | token 中位数 g / b / ba / s |
|---|---|---|---|---|---|---|
| T1 | 14 | 10 / 0.82 | 13 / 0.96 | 13 / 0.93 | 10 / 0.82 | 32k / 23k / 39k / 18k |
| T2 | 16 | 10 / 0.78 | 12 / 0.84 | 10 / 0.75 | 6 / 0.47 | 31k / 38k / 41k / 23k |
| T3 | 3 | 2 / 0.83 | 0 / 0.33 | 1 / 0.50 | 0 / 0.50 | 51k / 65k / 69k / 39k |
| 合计 | 33 | 22 / 0.80 | 25 / 0.85 | 24 / 0.80 | 16 / 0.62 | 33k / 38k / 43k / 21k |

- 配对比较：bounded 对 single 10:1（p = 0.012），bounded_all 对 single 8:0（p = 0.008），generalist 对 single 8:2（p = 0.11）；三种全覆盖策略两两之间 p ≥ 0.45。
- 剔除越界任务后剩 17 个：generalist 14、bounded 13、bounded_all 13、single 9。
- 平均对话 4.4–5.0 轮，每段对话消耗 2 万–7 万 token，约为单轮场景的 5 倍。
- 难度：最好的策略 pass^1 为 0.85、pass^2 为 25/33，比单轮评测低得多，与 τ-bench 报告的 pass^k 下降趋势一致。
- single 主要失败在路由：资产排查被交给没有 `lookup_asset` 的 Agent（17 次），错误码同理（11 次）。

### 7.4 结论与边界

- 修复后多 Agent（bounded）与通用 Agent 成功率没有显著差异，两者都显著优于按意图路由的单个专职 Agent。修复前多 Agent 落后的原因是规划器的覆盖缺陷和副作用，不是“多 Agent 必然更差”。
- `bounded_all` 没有带来额外收益，token 却最多：多 Agent 修复后的瓶颈已经不在工具覆盖。
- 默认使用 `generalist`：成功率相当，token 更少，链路更简单。`bounded` 保留给需要按领域隔离写权限、拆分提示词和监控的部署。
- 重复建单是这套评测发现的最大问题，而且出在四种策略共用的审批层，在 Agent 架构之外。按最终状态评分的多轮评测，比单轮“是否调用某工具”的评测更能暴露真实缺陷。
- 局限：
  - v4 的 T3 只有 3 个有效任务、P 层没有数据，复杂任务和违规请求上的策略差异本次没有结论；
  - 模拟用户和 agent 是同一个模型；
  - 任务和 SOP 由作者编写，原子类型分布不均（解锁只出现 4 次）；
  - 每格 2 次重复，区间较宽。

## 未完成

- 未重跑的部分：v4 的 T3 后 9 个任务和 P 层，第 6 节场景修复后的 M/N 层。本文结论只基于已完成的部分；复现命令见 README，并发不要超过模型服务商的并发上限。
- bounded 预算是波次间检查而非硬上限；需要硬上限时应在单步内按 token 计量中断。
- 范围更窄的新申请不会替代范围更宽的待审批单（例如先“建单 + 权限”、后“只建单”），这种情况仍可能出现重复工单。
- 模型 token 级流式、令牌吊销、速率限制、多租户数据隔离未实现。
