# IT-Copilot 改进计划（交给执行者）

本计划覆盖八个方向：跨天任务的可靠性、共享状态并发、模型路由、评测闭环、P99 与成本、长期记忆治理、Agent 注册与灰度、成功率波动归因。共 9 个工作包（WP），按优先级排列。每个 WP 都是 `main` 上一组独立的提交：代码 + 测试 + ADR + CHANGELOG。

---

## 交付状态与简化约束（2026-11-13 核对）

九个工作包的主要实现和本轮可执行验收已完成，详细证据见 [验收记录](improvement-acceptance.md)。**并非全部达标**：WP6 的 P99 下降 30% 目标未达成；用户确认暂无人工审阅线上样本，WP8 发布门禁保持阻塞。画像角色和应用失败样本保留为质量问题。

- 优先保持单 Agent、单应用与现有数据库；不新增基础设施、通用插件框架或没有第二个实际用途的抽象。
- 模型请求预算由 DeadlineModel 管理，生成流程由 BaseAgent 管理；审批持久化与后台动作执行沿用各自入口，不将业务状态写入模型包装层。
- WP6 的有效配对报告已完成，效果与已知成本门禁通过，P99 目标未达标；Jev 阈值实验未实施。
- WP3 真实进程故障评测 24/24。最终 SQLite 728 通过、PostgreSQL 733 通过、真实 Keycloak/Gitea 集成 3/3；缺少真实样本的发布门禁不冒充通过。
- 交付按工作包整理；完整原始报告保留本地，CI 必需的候选资格摘要和验收摘要纳入版本控制。

---

## 0. 执行规则（先读，必须遵守）

### 0.1 前置条件

- 仓库只保留 `main` 一个分支，直接在 `main` 上开发，不新建分支。每个 WP 一个或几个提交，提交前 lint 和测试必须通过。
- 多租户阶段 0–3（ADR 0022–0025、迁移 0003–0005）已经合入 `main`，是本计划的基线。不要回滚或顺手修改这部分代码，除非某个 WP 明确要求。
- 新迁移编号从 `0006` 开始，ADR 编号从 `0026` 开始，同时更新 `docs/adr/README.md` 索引。
- 每个 WP 结束时运行：`ruff check .`、`.venv/bin/pytest -q`；有 PostgreSQL 契约测试时再设置 `IT_COPILOT_TEST_DATABASE_URL` 跑一遍。

### 0.2 编码规则：不要防御性编程

本仓库的风格是“边界校验、内部信任”。**以下写法一律不要出现**：

1. **不吞异常**：不写 `try: ... except Exception: pass` 或 `except Exception: logger.warning(...)` 之后继续执行。只捕获你能**具体处理**的异常类型，而且捕获后要有明确的下一步动作（例如 `IntegrityError` → 说明这个资源已被另一个 worker 占用，跳过）。其他异常直接抛出。
2. **不探测内部对象的形状**：不对本仓库自己的对象用 `getattr(x, "y", None)`、`hasattr`、`isinstance` 做兜底判断，类型和契约以函数签名为准。
3. **不加空值兜底**：字段在 schema 里本来不可空，就不写 `x or {}`、`x or []`、`x.get("k", default)`。
4. **不写兼容分支**：不为“旧数据可能没有这个字段”加分支。确实有旧数据，就写 Alembic 迁移一次性回填。
5. **配置错误要启动失败**：新增的 YAML 或环境变量不合法时，启动直接报错，不悄悄退回默认值。每个默认值只在一处声明。
6. **不重复校验**：上游已经校验过的输入（JSON Schema、策略、Pydantic 模型），下游不再校验一遍。
7. **不给每个功能都加开关**：只有本计划明确写了“加开关”的地方才新增环境变量。
8. **不预先抽象**：没有至少两个实现在本计划里落地，就不新建基类、工厂、插件机制。
9. **测试测真实行为**：数据库测试走 SQLite + PostgreSQL 契约测试；模型调用用 `pydantic_ai.models.function.FunctionModel` 或 `evaluation/fake_model.py`。不要 mock 本仓库自己的类。
10. **不顺手重构**：不改与本 WP 无关的代码、格式或命名。已有代码里的兜底写法，只有在本 WP 必须修改那一行时才顺带清理。
11. 注释只写“为什么”，不写“做了什么”；保持现有中英混用的注释风格。

**真正需要校验的系统边界**只有这些：HTTP 请求体、JWT、模型输出、外部适配器和 MCP 的返回值、配置文件加载、数据库唯一约束冲突。

### 0.3 交付物格式

每个 WP 都要有：

- 代码和迁移；
- 测试：新行为的正向用例 + 关键反例；
- `docs/adr/00NN-*.md`：背景、决定、后果、验证方法；
- `CHANGELOG.md` 条目；
- 如果 README“30 秒看懂”或“工程设计重点”里的表述受影响，同步修改。
- 有评测的 WP，把报告写到 `evaluation/reports/`，并在 ADR 里写明复现命令。**不要编造数字**：评测没跑，就写“未运行”。

---

## 1. 现状速览（不要重复造已有的东西）

| 方向 | 已有（勿重写） | 缺口（本计划要补的） |
|---|---|---|
| 跨天任务 | outbox + 租约 worker + CAS（`workflow/store.py`、`workflow/worker.py`）；结果未知时先回查（ADR 0004）；saga 补偿；到期回收；共享事项回合检查点 + 冻结输入 + 目标核验（ADR 0015）；`run_leased` | 授权漂移只在**回收那一刻**才发现；核验器只有 `access_level` 一种；卡在 UNKNOWN/MANUAL_REVIEW 的动作没有老化告警；worker 真实 SIGKILL 故障评测缺失 |
| 并发与一致性 | 审批按 `version` CAS；同一审批内按 seq 串行；会话锁 `redis_lease`；同会话的新申请替代旧申请 | **不同审批作用于同一资源（同一人 + 同一系统）时没有串行化**；**重叠的限时授权会算错 baseline**（见 WP1）；跨会话的重复申请对审批人不可见 |
| 模型路由 | `core/llm.build_model` 只有一个全局模型；意图识别 Jev → LLM → 规则三级回退；Agent 思考档位 | 没有按调用角色、租户合规、成本、健康状况选择模型；没有故障切换；trace 里不记录用了哪个模型、为什么用它 |
| 评测闭环 | 大量离线集与报告、pass^k、AgentDojo、人工体验评分 | **线上没有反馈入口和 badcase 回流**；trace 在 Redis 里只存 7 天；没有标注流程；没有“线上样本 → 回归集”的通路 |
| P99 与成本 | 前缀缓存优化、思考档位、阶段耗时指标、token 审计 | 单次模型请求超时 90 s × 最多 5 轮，**没有整轮截止时间**；P99 没有持续度量；评测脚本的 `percentile` 下标向下取整，有偏差；成本没有按租户和成功事项统计 |
| 长期记忆 | 工作记忆 / 情景记忆 / 画像三级；召回时有距离门槛和半衰期衰减；画像按字段修订；保留期清理 | **情景记忆把用户原话（可能含密码）直接向量化入库**（`api/routes/chat.py:309` → `store_episodic_turn`）；准入只排除了问候和闲聊；没有近重复合并；脱敏规则分散在三处（`core/redaction.py`、`agents/prompting.USER_SECRET`、`_normalize_profile` 的关键词）；用户不能查看或删除自己的记忆 |
| Agent 规模化 | 单 Agent（评测后的决定）；技能热加载；外部 MCP 白名单 | 提示词、技能、工具、模型的变更没有版本概念，无法灰度、无法按版本回滚，也无法在 trace 里区分版本 |
| 波动归因 | Prometheus 指标、OTel、Redis trace、SLO 燃烧率 | trace 里缺版本指纹（prompt/skill/model/policy/kb/git sha）；工具失败只有 `error` 一个桶；**没有变更事件记录**；没有归因工具 |

---

## WP1（P0）同一资源的并发串行化与重叠授权

**原则**：多个执行者共享同一份业务状态时，要做到三点：① 在**资源粒度**上串行化写入（同一个资源同一时刻只有一个执行者）；② 用版本或状态做比较再写（CAS）来检测冲突；③ 回滚时恢复到**根据台账算出来的期望状态**，而不是某个动作冻结时看到的快照。

### 1.1 先写失败的测试（确认缺陷）

在 `tests/test_workflow.py`（或新建 `tests/test_overlapping_grants.py`）里用本地适配器写三个用例。**先跑一遍确认它们现在失败**，再动代码：

1. **同角色重叠**：审批 A 给工号 1001 授予 gitlab `developer` 24 h，执行成功；随后审批 B 授予同一个 `developer` 72 h。当前行为：B 被记为 `PREEXISTING`，不会安排回收；24 h 后 A 回收，把权限恢复到“无”，**B 授予的权限被提前收走**。期望：24 h 时权限保持 `developer`，72 h 时恢复为“无”。
2. **升级重叠**：A 授予 `reporter` 24 h（原本没有权限）；B 授予 `developer` 72 h，此时 B 的 baseline 被记成 `reporter`，而这个 reporter 其实是 A 临时给的。当前行为：24 h 时 A 发现 current ≠ reporter，判为漂移并转人工；72 h 时 B 恢复到 `reporter`，**临时权限变成了永久**。期望：24 h 时不动（developer 仍被 B 覆盖），72 h 时恢复为“无”。
3. **并发**：两个 `ExecutionWorker` 同时领取作用于同一个 (tenant, employee, system) 的 apply 和 revoke，断言二者不会同时处于 EXECUTING。这一条在 PostgreSQL 上跑。

### 1.2 资源键与串行化

- 在 `workflow/actions.py` 里新增 `resource_key(tenant, tool_name, arguments) -> str`，每个写动作都必须映射到一个键：
  - `request_access` / `revoke_access`：`f"{tenant}:access:{employee_id}:{system}"`
  - `reset_password` / `unlock_account`：`f"{tenant}:account:{employee_id}"`
  - `lock_device`：`f"{tenant}:device:{device_id}"`
  - `create_ticket`：`f"{tenant}:ticket:{action_id}"`（工单之间互不冲突，键唯一即可）
- 迁移 `0006_action_resource_key.py`：给 `actions` 加非空列 `resource_key`，按上面的函数回填存量行；建部分唯一索引 `uq_actions_resource_executing ON actions(resource_key) WHERE status = 'executing'`（SQLite 和 PostgreSQL 都支持部分索引）。
- 所有插入 `actions` 的地方（`_enqueue`、`succeed` 里生成的 revoke、`_carry_over`）都写入 `resource_key`。
- `WorkflowStore.claim`：把行 CAS 成 EXECUTING 时如果撞上唯一索引，捕获 `sqlalchemy.exc.IntegrityError`，跳过这一行（说明这个资源已被别的动作占用），保持 QUEUED 等下一轮再领。这是唯一允许新增的 `except`。租约过期的 EXECUTING 行会被重新领取（同一行），不会和自己冲突。

### 1.3 期望状态由台账计算

- 在 `WorkflowStore` 里新增 `desired_level(db, tenant, employee_id, system, *, excluding_action_id=None) -> Optional[str]`：期望等级 = max(这个资源上**最早一笔**仍然 ACTIVE 的授权记下的 `baseline_role`，所有仍然 ACTIVE 的授权的 `role`)。没有 ACTIVE 授权时返回 None，表示“由外部系统自行决定，我们不管”。
- `request_access` 的 baseline：如果这个资源上已有我们自己发出的 ACTIVE 授权，baseline 取最早那笔授权的 `baseline_role`，**不取** `current_level`。
- `preexisting` 判定只适用于“baseline 本身已经满足”的情况。如果当前等级满足要求只是因为我们自己另一笔授权还没到期，新授权仍然记为 ACTIVE 并安排回收，但不调用适配器的 `apply`（结果里标 `covered_by: <action_id>`）。
- `revoke_access`：执行时不再使用冻结在参数里的 `baseline_level`，而是在同一个事务里算出 `restore_to = desired_level(..., excluding_action_id=本授权)`。漂移判定改为 `current != 回收前的期望等级`（即包含本授权在内的期望等级），而不是 `current != 本授权的 role`。
- `revoke_access` 适配器参数改为显式的 `restore_to`。本地适配器和 Gitea 适配器两处都要改；`baseline_level` 参数删掉，不做兼容。
- saga 补偿生成的 revoke 走同一条路径。

### 1.4 跨会话冲突对审批人可见

- `WorkflowStore._body`（读取时计算，不落库）：用 `resource_key` 查出同一租户内**其他**状态为 DRAFT/PENDING/APPROVED 且尚未执行完的审批，返回 `conflicts: [{approval_id, status, requester, conv_channel}]`。
- 审批卡片 `frontend/src/components/ApprovalCard.vue` 显示“同一资源还有 N 个在途申请”。**不要自动替代**跨会话的申请：不同申请人提交的可能都是合理的申请，由人来决定。

### 1.5 验收

- 1.1 的三个用例通过，并在 PostgreSQL 上通过。
- 现有工作流测试全部通过：并发批准、双人投票、回查、崩溃恢复、saga、漂移保护、替代。
- ADR 0026「资源级串行化与台账期望状态」，并修订 ADR 0003/0004 中关于回收的描述。

---

## WP2（P0）持久化的回合记录、版本指纹与变更事件

WP4、WP5、WP6、WP8、WP9 的数据都来自这里，必须先做。

**原则**：要做归因，每一轮都必须能回答“用的是哪个版本的什么东西”。工具返回值要有可分组的错误类别。所有会改变 Agent 行为的变更都要留下带时间戳的事件。

### 2.1 `turn_records` 表（迁移 `0007_turn_records.py`）

| 列 | 说明 |
|---|---|
| `request_id` PK | 与现有 trace_id 相同 |
| `tenant`, `owner`（scope_key）, `channel`（web/feishu/task）, `conv_id`, `task_id` 可空 | |
| `created_at` | |
| `status`, `error_code`, `outcome` | `outcome` 由 2.4 节的 `turn_outcome()` 计算 |
| `intent`, `intent_group`, `intent_source`, `intent_confidence` | |
| `agent_spec`, `agent_version` | WP8 之前固定写 `it_copilot` / `build:<git_sha>` |
| `model`, `route`, `route_reason` | WP5 之前固定写当前模型名、`agent`、`primary` |
| `prompt_fp`, `skill_fp`, `tools_fp`, `policy_version`, `kb_version`, `build_sha` | 见 2.2 |
| `model_requests`（轮数）, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cost_usd` 可空 | 轮数取 PydanticAI `usage.requests` |
| `latency_ms`, `first_token_ms` 可空, `stage_ms` JSON | |
| `tools` JSON | `[{name, outcome, error_class, latency_ms}]`，不存入参和出参 |
| `signals` JSON | 自动 badcase 信号，见 WP4 |
| `approval_id` 可空 | |

- 写入位置：`api/routes/chat.py::_complete_turn_run`（与 `commit_turn` 同一处，写 PostgreSQL/SQLite）和 `agents/task_runner.py` 所在的共享事项回合结算处（`TaskStore.finish` 的同一事务里）。
- 渠道（飞书）走 `_channel_turn`，复用同一条写入路径。
- 加入 `core/retention.py`：`IT_COPILOT_RETENTION_TURN_DAYS`，默认 90 天。
- 不存消息正文：正文仍在 Redis trace 里（7 天）；需要长期保留的样本由 WP4 拍快照。

### 2.2 版本指纹

- `BaseAgent.fingerprints() -> dict`：`prompt_fp` = sha256(`system_prompt` + `AGENT_RULES`)；`tools_fp` = sha256(按工具名排序后的 [name, input_schema] 列表)；`skill_fp` = SkillManager 当前快照内容的 sha256（加在 `core/skill_loader.py`，`reload()` 后更新）。只在 `_runtime()` 重建时计算一次。
- `policy_version`：取 `WorkflowStore.policy.version`。
- `kb_version`：取当前租户知识库集合的版本（`vector_documents` 中该集合最大的 `updated_at`，检索缓存已经在用这个值），由 `tooling/knowledge_base.py` 暴露为 `collection_version(tenant)`。
- `build_sha`：环境变量 `IT_COPILOT_BUILD_SHA`，由 `Dockerfile` 的 `ARG BUILD_SHA` 注入；`start_local.sh` 用 `git rev-parse --short HEAD` 设置。未设置时启动失败，测试里在 `tests/conftest.py` 中设置。
- OTel：`monitor/telemetry.chat_span` 设置属性 `it_copilot.agent_version`、`it_copilot.model`、`it_copilot.route`、`it_copilot.policy_version`、`it_copilot.tenant`。

### 2.3 工具错误类别

- 在 `tooling/executor.py` 定义少量异常类：`ToolInputInvalid`（Schema 校验失败）、`WriteGuardBlocked`（`_guard_unsafe_tool` 的各种拒绝）、`PolicyDenied`（策略拒绝；找到写工具里抛出策略错误的位置，改抛这个类），以及已有的 `ToolTimeout`、`ToolUnavailable`。
- 把 `BaseAgent._invoke_tool` 里的 `outcome` 映射改为 `error_class ∈ {success, input_invalid, guard_blocked, policy_denied, timeout, circuit_open, upstream_4xx, upstream_5xx, error}`。外部 MCP 与 HTTP 适配器按状态码归入 `upstream_4xx` / `upstream_5xx`。
- `metrics.AGENT_TOOL_CALLS` 的标签 `outcome` 改为 `error_class`。同时修改 `docs/` 里引用这个标签的告警或说明。

### 2.4 成功判定只有一个定义

`agents/outcome.py::turn_outcome(record) -> str`，取值 `success | partial | failed | false_claim_noted | round_limit | model_unavailable`。评测脚本、归因脚本、灰度分析都调用这个函数，不各自定义“成功”。

### 2.5 `change_events` 表（同一迁移）

列：`id`、`at`、`tenant` 可空（平台级变更）、`kind`、`ref`、`detail` JSON、`actor`。

`kind` 取值和写入点：

| kind | 写入点 |
|---|---|
| `deploy` | API 启动时，`build_sha` 与上一条 deploy 不同时写入（`api/main.py` lifespan） |
| `policy` | `workflow/worker.py::_record_policy_loaded` |
| `skills` | `core/skill_loader.reload`，指纹变化时写入 |
| `knowledge` | `api/routes/knowledge.py` 的重建/上传接口 |
| `mcp_tools` | `AgentOrchestrator.set_external_tools`，工具集合变化时写入 |
| `model_route` | WP5 路由表重载时 |
| `agent_rollout` | WP8 灰度变更时 |

- 统一入口 `core/change_log.py::record_change(engine, kind, ref, detail, *, tenant=None, actor="system")`。
- `GET /admin/changes?since=&kind=`，需要 `it_admin` 或 `auditor` 角色。

### 2.6 验收

- 测试：一轮 `/chat` 结束后 `turn_records` 有一行，字段完整；修改技能后 `skill_fp` 变化且 `change_events` 多一条 `skills`；工具 Schema 失败记为 `input_invalid`，写守卫拒绝记为 `guard_blocked`。
- ADR 0027「回合记录、版本指纹与变更事件」。

---

## WP3（P1）跨天任务：漂移巡检、核验器补全与真实故障评测

**原则**：跨天任务的可靠性靠四样东西——持久状态机（已有）、幂等执行键（已有）、周期性对账（缺）、按业务状态验收（只覆盖了权限）。最终一致性不是“执行成功就完事”，而是要持续比对“台账认为的状态”和“目标系统的实际状态”。

### 3.1 授权漂移巡检

- 新增 `workflow/reconciler.py::GrantReconciler`，在 worker 进程里周期运行（`IT_COPILOT_RECONCILE_INTERVAL_S`，默认 900）：
  - 按租户分批读取 ACTIVE 授权（每批 50 条）；
  - 对每个资源调用 `adapter.current_level`，与 WP1 的 `desired_level` 比较；
  - 不一致时：授权记录标记为 `DRIFTED`，取消对应的待执行 revoke 动作（状态 CANCELLED，原因“巡检发现漂移”），写审计 `grant_drift_detected`，计数 `it_copilot_grant_drift_total{system}`。如果关联了共享事项，通过已有的 `TaskStore.ingest` 发一条事件到该事项。
  - 适配器抛出 `TransientError` 或 `AmbiguousError` 时，这一条本轮跳过、计数 `it_copilot_reconcile_skipped_total`；其他异常照常抛出。
- 巡检**只观察、不修改**目标系统；修复交给人。

### 3.2 卡住的工作要告警

- `monitor/performance_monitor.py` 加两条规则，数据由 worker 每轮汇报：
  - `oldest_unknown_action_age_s`：处于 UNKNOWN 或 MANUAL_REVIEW 最久的动作，超过 1 h 为 warning，超过 24 h 为 critical；
  - `oldest_queued_due_action_age_s`：已到执行时间（`not_before` 已过）但仍在排队的动作，超过 10 min 为 warning（worker 停了或被 WP1 的资源锁卡住了）。
- 对应 Prometheus Gauge：`it_copilot_workflow_oldest_age_seconds{state}`。

### 3.3 目标核验器补全

在 `workflow/goal_verifiers.py` 中按现有 `GoalVerifier` 端口新增两个核验器，并在组合根注入：

- `device_locked`：读取设备台账中的锁定状态；
- `ticket_open`：工单存在且状态不是关闭。

### 3.4 worker 真实 SIGKILL 故障评测

扩展 `scripts/evaluate_long_tasks.py`（不新建脚本），新增三个场景，每个重复 3 次：

1. `adapter.apply` 返回之后、`store.succeed` 之前杀掉 worker：期望新 worker 进入 UNKNOWN → observe → reconciled，适配器的 apply 总调用次数为 1；
2. 批量审批第 2 个动作执行中杀掉：期望恢复后继续，或按 saga 补偿，不重复授予；
3. revoke 执行中杀掉：期望恢复后权限等于 `desired_level`。

做法沿用现有的子进程 + 逻辑租约时钟。报告写入 `evaluation/reports/long_tasks_worker_kill_<date>.json`。

### 3.5 明确不做

- **不把聊天回执从 Redis 迁到数据库**。回执 24 h TTL 足够覆盖客户端重试；跨天的状态（审批、动作、事项、飞书收件箱）本来就在 PostgreSQL。迁过去会破坏工作记忆与回执在同一个 Redis 事务里写入的原子性。把这个取舍写进 ADR。
- 不引入 Temporal 等工作流引擎：outbox + 租约 + 检查点已经覆盖了需求。

### 3.6 验收

- 测试：在 Gitea 或本地目录里手工改权限后跑一次巡检 → 授权记录变成 DRIFTED、revoke 被取消、有审计；两个新核验器的 passed / failed / unknown 三种结果。
- 3.4 的报告。
- ADR 0028「周期对账与卡住工作告警」。

---

## WP4（P1）线上 Badcase 回流、人工标注与回归闭环

**原则**：闭环有五步——线上自动抓取信号 + 用户显式反馈 → 拍快照（trace 会过期）→ 按根因分类法人工标注 → 转成离线回归用例 → 版本发布前跑回归作为门禁。光看任务完成率不够，还要看正确性、过程质量、体验、成本、稳定性五类指标（见 4.6）。

### 4.1 用户反馈

- 迁移 `0008_feedback_badcases.py`。`feedback` 表：`request_id`、`tenant`、`actor`、`rating`（+1/−1）、`reason`（`wrong_answer | not_done | false_claim | slow | wrong_language | unsafe | other`）、`comment`（经 `core/redaction` 脱敏，最多 500 字）、`created_at`；唯一约束 (`request_id`, `actor`)，重复提交时覆盖。
- `POST /chat/{request_id}/feedback`：只有 `turn_records.owner` 等于当前主体才能提交，复用 `api/routes/ops.py::_owns_trace` 的判断方式。
- `POST /tasks/{task_id}/events/{event_id}/feedback`：事项成员可以提交，观察者也可以。
- 前端：`ChatMessage.vue` 和 `TaskConversation.vue` 的 Agent 回复下面加 👍/👎；点 👎 后选择原因并可填写备注。

### 4.2 自动信号（写入 `turn_records.signals`）

**回合结束时**由 `_complete_turn_run` 和任务结算计算：

`claim_corrected`、`claim_noted`、`round_limit`、`bad_output`、`model_unavailable`、`tool_error`（error_class 不是 success / guard_blocked / policy_denied）、`answer_reset`、`escalated`、`intent_rule_fallback`（`recognition_source` 为关键词规则）、`deadline`（WP6）。

**事后**由 4.3 的挖掘任务从业务表关联计算：

- `draft_withdrawn`：草稿被撤回或过期未确认；
- `approval_rejected`；
- `verification_failed`；
- `task_reopened`；
- `clarify_loop`：同一会话连续 3 轮以上缺槽位；
- `rephrase`：同一会话 60 秒内下一条用户消息与本条用户消息的 embedding 余弦距离 < 0.15。用 `core/embedding.py`，正文从 Redis 工作记忆读取，必须在 24 h 内挖掘。

### 4.3 Badcase 挖掘与快照

- 新增 `workflow/badcase_miner.py::BadcaseMiner`，每 10 分钟在 worker 进程里运行一次：
  - 来源一：上次运行之后、带任一信号的 `turn_records`；
  - 来源二：负反馈；
  - 来源三：成功回合按 1% 随机抽样（`source=sampled`），用来估计漏检率。
- `badcases` 表：`id`、`request_id`（唯一）、`tenant`、`source`（feedback/signal/sampled）、`signals`、`status`（`new → triaged → labeled → promoted | dismissed`，用 `core/states.Machine` 声明）、`snapshot` JSON、`label` JSON、`labeler`、`labeled_at`、`second_label` JSON 可空、`dataset_case_id` 可空。
- `snapshot`：建 badcase 时从 Redis trace 复制一份（已脱敏），外加本轮用户消息和回复正文（经 `core/redaction.redact_text`，见 WP7）。Redis trace 已过期就只存 `turn_records` 字段，并标记 `snapshot_partial: true`。

### 4.4 标注

- 根因分类法写在 `evaluation/badcase_taxonomy.py`：`intent_wrong`、`slot_missing_or_default`、`retrieval_miss`、`retrieval_irrelevant`、`tool_not_called`、`tool_wrong_args`、`false_claim`、`policy_explain`、`language_tone`、`latency`、`model_error`、`product_gap`、`not_a_bug`。
- 一条标注 = `{root_cause, severity: P0..P3, expected: {intent?, must_call?: [tools], must_not_call?: [tools], outcome?: grant|ticket|none|..., forbid_text?: [...]}, note}`。`expected` 的字段名与 `evaluation/datasets/application_*.json` 的用例字段一致（先读 `evaluation/application.py` 和 `scripts/evaluate_application.py` 确认字段，以它们为准）。
- API（`it_admin` 角色）：
  - `GET /admin/badcases?status=&root_cause=&since=`
  - `GET /admin/badcases/{id}`
  - `POST /admin/badcases/{id}/label`
  - `POST /admin/badcases/{id}/dismiss`
  - `POST /admin/badcases/{id}/promote`
- 双人标注：`sampled` 来源的 badcase 有 10% 随机要求第二人标注（不能是同一个人）。`scripts/quality_report.py` 输出 root_cause 的 Cohen's kappa。
- 前端：`OperationsView.vue` 新增「Badcase」页签，列表 + 详情（快照、信号、标注表单）。只用现有组件风格。

### 4.5 转回归用例

- `scripts/export_badcases.py --status promoted --output evaluation/datasets/online_regression.json`：把 promoted 的 badcase 转成 `evaluate_application.py` 能读的用例（`turns` 取快照里的用户消息，期望取 `label.expected`）。去掉工号以外的个人信息，工号统一映射到演示目录里的工号。
- 这个文件由人审阅后提交进 git。脚本不自动提交。
- 门禁分两层：
  - CI（离线、确定性）：promoted 用例中 root_cause 属于 `intent_wrong`、`slot_missing_or_default`、`false_claim`、`tool_not_called` 的部分，用 `evaluation/fake_model.py` 能确定性复现的就加入 `tests/test_online_regressions.py`；不能确定性复现的不进 CI。
  - 发布前（真实模型）：`scripts/evaluate_application.py --dataset online_regression --repeats 3`，WP8 的版本上线检查要求附带这份报告。

### 4.6 核心指标

`scripts/quality_report.py --since --until [--tenant] [--agent-version]` 从 `turn_records`、`feedback`、`badcases` 和工作流表计算以下指标，写入 `evaluation/reports/quality_<date>.json`，并与上一份报告对比：

| 类别 | 指标 |
|---|---|
| 正确性 | 业务目标核验通过率；虚假声明率（claim_noted / 写操作回合）；越权写入数（必须为 0）；审批被拒率与草稿撤回率（反映起草质量）；事项重开率 |
| 过程 | 达成目标所需轮数（中位数、P90）；澄清率与 clarify_loop 率；工具错误率（按 error_class）；平均模型请求数与 round_limit 率；意图回退率（LLM / 规则）；检索证据率（有引用的回答占比）；转人工率 |
| 体验 | 首字与整轮时延的 P50/P95/P99；answer_reset 率；负反馈率与反馈覆盖率；rephrase 率 |
| 成本 | 每轮成本；**每个成功事项的成本**；缓存命中率（cache_read / input）；每轮 token 的 P95 |
| 稳定性 | pass^1 与 pass^3 的差距（离线）；版本间差异的置信区间（Wilson） |

同时修掉 `scripts/evaluate_application.py:48` 等五处 `percentile` 的下标偏差：统一放到 `evaluation/reporting.py::percentile`，用最近秩法（nearest-rank，`ceil(q*n)-1`），删掉五份副本。

### 4.7 验收

- 测试：带 `claim_noted` 信号的回合 → 挖掘后生成 badcase 且快照完整；负反馈 → 生成 badcase；标注 → promote → 导出的用例能被 `evaluate_application.py` 读取并执行（用 fake_model 跑通）。
- ADR 0029「线上 badcase 回流与标注闭环」。

---

## WP5（P1）模型路由

**原则**：路由是确定性代码，不是让模型来选模型。顺序是：先按**硬约束**过滤（合规的数据等级和地域、能力是否支持、健康状况、预算），再在剩下的候选里按**离线评测确定的偏好顺序**选。“效果”体现在“哪些模型有资格进候选列表”（评测门禁），“成本”体现在偏好顺序，“延迟和健康”在运行时动态调整。不上在线学习（bandit），因为没有真实流量，学不出来。

### 5.1 配置 `config/model_routes.yaml`

```yaml
models:
  deepseek-flash:
    base_url_env: OPENAI_BASE_URL
    key_env: OPENAI_API_KEY
    name: deepseek-chat
    price_per_mtok: {input: 0.27, output: 1.10, cache_read: 0.07}   # 以官方价格页为准，填写时注明查询日期
    regions: [cn]
    data_classes: [public, internal]
    supports: [tools, tool_choice_required]
  # 第二个模型由仓库所有者提供 key 后再加；没有 key 就只配一个，路由退化成当前行为
routes:
  intent_fallback: {candidates: [deepseek-flash], timeout_s: 3}
  agent_read:      {candidates: [deepseek-flash], thinking: disabled, max_rounds: 3, latency_slo_s: 6}
  agent_write:     {candidates: [deepseek-flash], thinking: disabled, max_rounds: 5, latency_slo_s: 10}
  summary:         {candidates: [deepseek-flash]}
  profile:         {candidates: [deepseek-flash]}
  rewrite_rerank:  {candidates: [deepseek-flash]}
  judge:           {candidates: [deepseek-flash]}
tenants:
  default: {data_class: internal, regions: [cn]}
```

- 加载时校验：引用的模型都存在；route 要求的能力（agent 和 intent 需要 `tool_choice_required`）都被候选模型支持；每个租户至少有一个合规的候选。任何一项不满足就启动失败。
- `max_rounds` 和 `thinking` 取代全局的 `IT_COPILOT_MAX_MODEL_ROUNDS` 和 `IT_COPILOT_AGENT_THINKING`，**删除这两个环境变量**，同步修改 `.env.example` 与文档。
- 热加载：与策略文件一样按 mtime 重载，重载成功后写 `change_events(kind=model_route)`。

### 5.2 `core/model_router.py`

```python
@dataclass(frozen=True)
class Routed:
    model: Model          # PydanticAI Model
    name: str             # models 的键
    route: str
    reason: str           # primary | latency_shift | failover | budget
    settings: ModelSettings

class ModelRouter:
    def pick(self, route: str, tenant: str) -> Routed: ...
```

选择步骤：

1. `routes[route].candidates` 按租户的 `data_class` 和 `regions` 过滤；
2. 去掉熔断为 open 的模型（每个模型一个 `tooling.tool_manager.CircuitBreaker`，`is_model_unavailable` 判定的失败计入）；
3. 如果第一个候选最近 5 分钟的 P95 > `latency_slo_s`，并且第二个候选健康，选第二个，`reason=latency_shift`；
4. 租户当天成本超出 `tenants.quotas.daily_cost_usd`（从 `turn_records` 求和，每分钟缓存一次）时：`agent_write` 路由改为拒绝起草新的写操作申请并告知用户（走现有 `hidden_tools` 机制隐藏写工具），`agent_read` 照常；`reason=budget`。

- 故障切换：只在**尚未推送任何 answer_delta** 时，把 `is_model_unavailable` 的错误切到下一个候选重试一次（`reason=failover`）。已经开始流式输出就不切换，按现有的失败处理走。
- `agent_read` 和 `agent_write` 的划分：`IntentResult.requested_actions` 非空就走 `agent_write`，否则走 `agent_read`。`requested_actions is None`（Jev 没给判断）时走 `agent_write`，与现在“不限制写操作”的行为一致。
- 改所有 `build_model` 调用点，改为从 router 取模型：`AgentOrchestrator`、`IntentRecognizer`、`MCPToolManager`（改写、重排）、`memory/conversation_memory.py`（摘要、画像）、评测裁判。`core/llm.build_model` 只给 router 内部使用。
- `BaseAgent._call_llm` 每次调用都通过 `router.pick` 拿模型和 settings，`Agent.run(model=...)` 本来就支持按调用传入模型。
- 结果写入 `turn_records.model / route / route_reason / cost_usd`（cost 用价格表乘 token 数；cache_read 按缓存价计费，不再重复计入输入）。

### 5.3 评测门禁

- 新增 `scripts/evaluate_model_route.py --route agent_read --model X --repeats 3`：在 `evaluate_application.py` 的数据集上强制使用指定模型，输出成功率、P95、每个成功用例的成本，写入 `evaluation/reports/routing/<route>__<model>.json`。
- 新增 `scripts/check_model_routes.py`，在 CI 里运行：`model_routes.yaml` 里每个 (route, candidate) 都必须有报告，而且成功率不低于该 route 第一候选的值减 3 个百分点（用 Wilson 下界比较）。不满足 CI 就失败。
- 在 `docs/evaluation.md` 加一张成本-成功率-P95 的对比表，偏好顺序依据这张表决定。

### 5.4 验收

- 测试：合规过滤（confidential 租户只能选到声明了 confidential 的模型，没有这样的模型时启动失败）；熔断后跳过；流式输出之前切换、之后不切换；超预算时写工具被隐藏；配置非法时启动失败。
- ADR 0030「模型路由：硬约束过滤 + 评测决定偏好」。同时修订 `docs/multi-tenant-plan.md` 阶段 4 里“模型路由”和 `tenant_usage` 表的内容：改为复用 `turn_records`，不再新建 `tenant_usage`。

---

## WP6（P1）P99 延迟与单任务成本

**原则**：先度量再优化；先砍尾部的来源，再碰平均值。每一项优化都要满足“效果不劣”：在同一数据集上跑 3 遍，成功率的下降不超过 2 个百分点，或者落在置信区间内。

### 6.1 基线（先做，不改代码）

- `scripts/benchmark_stream.py` 加 `--concurrency N --repeats R`，输出首字和整轮的 P50/P95/P99，以及按阶段的 P99。
- 用 `application_strata_v3 + application_hard_v4` 跑 3 遍，加上 benchmark 并发 1/4/8，得到**基线报告** `evaluation/reports/latency_cost_baseline_<date>.json`。之后所有目标都相对于这份基线。

### 6.2 整轮截止时间（尾部的主要来源）

- 现在单次模型请求超时 90 s × 最多 5 轮，理论上一轮可以拖到 7 分钟以上。改为：
  - 每个 route 增加 `turn_deadline_s`（`agent_read` 默认 20、`agent_write` 默认 30）和 `request_timeout_s`（默认 15）；
  - `BaseAgent._call_llm` 用 `asyncio.timeout(deadline)` 包住整轮；每次请求的超时取 min(request_timeout_s, 剩余时间)。
  - 到达截止时间时：不再请求模型；如果本轮已经有写工具产生了审批或草稿，用 `fallback_note` 那套确定性文字告诉用户已经生成了什么；否则回复“处理超时，请重试”。`status=partial`、`error_code=deadline`，并写入信号 `deadline`。
- 删除 `IT_COPILOT_LLM_TIMEOUT_S`。

### 6.3 意图识别尾部

- `intent_fallback` 路由 `timeout_s: 3`。超时后直接走关键词规则，记 `intent_rule_fallback`。
- 实验项（必须过评测门禁才能合入）：Jev 置信度在 [0.5, 0.7) 并且 top1 属于只读意图组时，不再回退 LLM。在 `intent_v4_blind` 上严格口径的准确率不能下降。

### 6.4 轮数与纠正

- `agent_read` 的 `max_rounds: 3`、`agent_write` 的 `max_rounds: 5`，已在 WP5 配置；用基线数据集验证 `agent_read` 的 round_limit 率 < 1%。
- 在 `quality_report` 里统计 claim 纠正（第二次 run）的频率和它带来的时延；只度量，不改。

### 6.5 外部工具

- `config/mcp_servers.json` 的 `timeout_s` 由 25 降到 8。工具治理层已有熔断。

### 6.6 成本

- 依靠 WP5 的路由和每轮成本，`quality_report` 输出“每个成功事项的成本”。
- 前缀缓存命中率加入 `quality_report`。命中率比上一版本下降超过 10 个百分点时，WP9 的归因脚本会把 `prompt_fp` / `tools_fp` 变更列为首要嫌疑。

### 6.7 验收

- 对比报告 `latency_cost_after_<date>.json`：整轮 P99 相对基线下降 ≥ 30%；每个成功用例成本不上升；成功率满足“效果不劣”。达不到就如实写出原因，不调参硬凑。
- 测试：截止时间触发后，已生成的审批仍然如实告知用户；超时请求不会挂到 90 s。
- ADR 0031「整轮截止时间与尾延迟治理」。

---

## WP7（P1）长期记忆：准入、冲突消解、衰减与脱敏

**原则：放在写入侧还是召回侧？**

- **写入侧**：脱敏、准入、去重与冲突合并。原因是内容一旦被向量化或写进摘要，就无法可靠撤回；而且写一次就够，召回时每次都做既贵又容易漏。
- **召回侧**：相关度门槛、时效衰减、权限与作用域过滤、与权威数据核对。这几样依赖“当前的查询和当前的时间”，写入时还不知道。
- **后台**：硬过期删除（保留期）和用户主动删除。
- 记忆永远不能作为权限、工单、设备状态的依据；这些状态必须调用工具查询。

### 7.1 统一脱敏（写入侧）

- `core/redaction.py` 新增 `redact_text(text) -> str`，把三处规则合在一起：
  - `agents/prompting.USER_SECRET`（用户贴出的密码和验证码）；
  - 现有的 `sk-` 密钥和 Bearer 令牌；
  - 新增中国大陆手机号 `1[3-9]\d{9}`、身份证号 `\d{17}[\dXx]`、银行卡号（13–19 位连续数字，并做 Luhn 校验）。
  - 工号、设备号**不脱敏**，业务需要它们。
- `redact_user_secrets` 改为调用同一份规则；`_normalize_profile` 的关键词屏蔽改为 `redact_text`。
- 所有写入记忆的位置都先 `redact_text`：`commit_turn`（工作记忆）、`store_episodic_turn`、`_compress` 的摘要输入和输出、画像提炼的输入。Redis trace 已经有 `redact`，额外对其中的字符串字段做 `redact_text`。
- 一次性脚本 `scripts/redact_existing_memory.py`：重新脱敏已有的情景记忆和画像文档，脱敏后内容变化的文档重新生成 embedding。

### 7.2 准入（写入侧）

新增 `memory/admission.py::admit_turn(response, intent_result, conversation_state) -> Optional[EpisodicRecord]`，规则如下：

- 不入库：`status != success`；意图属于 greeting、chitchat、out_of_scope；本轮仍缺槽位（还没问完）；回复是“未检索到”一类的无结论回答（判断标准：`knowledge_used` 为假并且没有调用任何工具）；
- importance 按**业务事实**而不是模型文字确定：本轮生成了审批、草稿或工单 → 0.8；有知识引用的诊断 → 0.6；其他 → 0.4；
- 存的是结构化的“问题 + 结论 + 业务记录 ID”，而不是整段对话原文：`doc = 员工问题（脱敏，≤300 字）+ 回答要点（回复前 300 字，脱敏）`，`metadata` 加 `approval_id`、`ticket_id`、`importance`、`source_request_id`。
- `api/routes/chat.py:307` 的 if 条件改为调用 `admit_turn`。

### 7.3 去重与冲突（写入侧）

- 写入情景记忆前，在同一用户的记录里查最近邻：余弦距离 < 0.08 视为同一件事，更新那一条（`ts` 改为现在、`seen += 1`、`doc` 换成新的），不新增。
- 画像：每个字段增加 `updated_at` 和 `source_request_id`。`PROFILE_SCHEMA_VERSION` 改为 5，旧数据由 `_upgrade_profile` 转换（这个转换已经存在，扩展一下即可，不另写兼容分支）。字段级“新值替换旧值”的策略保持不变。
- 画像字段超过 180 天没有更新的，在召回时不再放进提示词（见 7.4），由下一次提炼重写。

### 7.4 召回侧

- 现有的距离门槛和半衰期衰减保留。衰减改用 `max(ts, last_seen)`，`seen` 作为 importance 的加成：`importance * (1 + 0.1 * min(seen, 5))`。
- 画像中超过 180 天的字段不进提示词。
- `MemoryContext.to_prompt_text` 里情景记忆段落的标题注明“历史对话摘要，可能已过时；权限、工单、设备状态以本轮工具查询为准”。先检查现有文字，已有类似表述就不改。

### 7.5 用户控制

- `GET /memory/me`：返回本人的画像和情景记忆列表（只含摘要和时间）。
- `DELETE /memory/me` 和 `DELETE /memory/me/episodic/{id}`：删除，并写一条 `admin_audit`（actor 为本人）。

### 7.6 评测

新增 `scripts/evaluate_memory_governance.py`，20 段确定性对话（fake_model），覆盖：贴出密码、手机号或身份证、闲聊、失败回合、缺槽位回合、同一件事问三次、部门变更、180 天前的旧画像。断言内容：

- 存储里不出现任何敏感模式（扫描向量表的 documents 和 metadata）；
- 不该入库的没有入库；
- 重复问题只有一条记录且 `seen = 3`；
- 部门变更后画像只保留新部门；
- 旧字段不进提示词。

再跑一次现有的 `memory_ablation`（`scripts/evaluate_long_dialogue.py`），确认召回带来的收益没有下降。

### 7.7 验收

- 上面的评测报告，以及单元测试。
- ADR 0032「长期记忆治理：写入侧脱敏、准入、去重，召回侧衰减」。

---

## WP8（P2）Agent 规格注册、能力路由、灰度与回滚

**原则**：不要因为“Agent 数量要过百”就去拆 Agent。本项目的评测已经证明多 Agent 没有收益（`docs/iteration-stories.md` §1）。规模化真正需要管理的，是**会改变行为的配置**：提示词、技能、工具白名单、模型路由、轮数上限。把这些打包成**带版本的 Agent 规格（AgentSpec）**，就能对规格做注册发现、按能力路由、灰度和一键回滚。以后真要加专职 Agent（例如安全事件），也只是多一个规格文件。

### 8.1 规格文件 `config/agents/it_copilot.yaml`

```yaml
name: it_copilot
version: 1.0.0
system_prompt: |            # 当前 BaseAgent.system_prompt 原文
  你是 IT-Copilot 的 IT 服务 Agent：...
rules: agents.prompting.AGENT_RULES     # 引用代码常量
tools: ["*"]                # 或显式白名单
skills: {root: skills/it_helpdesk, disclosure: full}
routes: {read: agent_read, write: agent_write}
serves: {intent_groups: ["*"]}
tenants: ["*"]
```

- 新增 `agents/registry.py::AgentRegistry`：从 `config/agents/*.yaml` 加载，规格本身不可变，按 (name, version) 索引。加载时校验：工具存在、route 存在、技能目录存在；`fingerprint` = 解析后完整内容的 sha256。出错就启动失败。
- `BaseAgent` 改为由 `AgentSpec` 构造：`system_prompt`、工具过滤、技能、route 名都来自规格，删掉类属性 `system_prompt`。`AgentOrchestrator` 按 fingerprint 缓存 `BaseAgent` 实例。
- **防漂移测试**：`it_copilot@1.0.0` 解析出来的 system prompt、工具集合、技能文本与重构前完全一致（在 fake_model 下对比 instructions 字符串）。

### 8.2 能力路由

- `AgentRegistry.resolve(tenant, intent_group) -> spec_name`：在 `serves.intent_groups` 和 `tenants` 都匹配的规格里，选**最具体**的那个（显式列出该意图组的优先于 `*`）。多个规格同样具体时启动失败，不允许出现歧义。
- 当前只有一个规格，所以这一步的实际结果和现在一样。

### 8.3 灰度与回滚

- 迁移 `0009_agent_rollouts.py`。`agent_rollouts` 表：`spec_name` PK、`stable_version`、`canary_version` 可空、`canary_percent`（0–100）、`canary_tenants` JSON 可空、`status`（`stable | canarying | rolled_back | promoted`，用 `Machine` 声明）、`updated_at`、`updated_by`。
- 分桶：`sha256(spec_name + scope_key) % 100 < canary_percent`，并且租户在 `canary_tenants` 内（为空表示不限制租户）。
- **粘性**：会话第一轮决定的版本写进 `conversation_state["agent_version"]`，后续轮次沿用；共享事项在创建时把版本写进事项数据。这样同一段对话中途不会换版本。
- 管理 API（`it_admin`，每次操作写 `admin_audit` 和 `change_events`）：
  - `GET /admin/agents`：规格、版本、fingerprint、灰度状态、能力；
  - `POST /admin/agents/{name}/canary {version, percent, tenants}`：要求该版本存在，并且 `evaluation/reports/release/<name>@<version>.json` 存在且通过（8.4）；
  - `POST /admin/agents/{name}/promote`；
  - `POST /admin/agents/{name}/rollback`：`canary_percent=0`、`status=rolled_back`。回滚不需要重新部署，各个 API 副本通过 5 s 的 TTL 缓存读到新状态（与 `TenantRegistry` 的做法相同）。
- 自动回滚：`monitor/performance_monitor.py` 加一条规则，每 60 s 运行，用 `turn_records` 按版本比较最近 30 分钟的数据。触发条件：canary 样本数 ≥ 100，并且以下任一成立——
  - `turn_outcome` 失败率超过 stable，差值的 Wilson 区间下界 > 0；
  - `claim_noted` 率 > stable 的 2 倍；
  - 负反馈率 > stable 的 2 倍；
  - 整轮 P95 > stable × 1.5。

  满足时自动执行 rollback（actor=`system`）并触发 critical 告警。越权写入相关的安全回归不依赖线上数据，在 8.4 的门禁里拦截。

### 8.4 上线门禁

- `scripts/release_check.py --spec it_copilot --version 1.1.0`：依次运行 `scripts/evaluate_security.py`（必须 0 越权）、`tests/test_online_regressions.py`、`evaluate_application.py --repeats 3`（成功率相对 stable 满足“效果不劣”）、`evaluate_application.py --dataset online_regression --repeats 3`，结果写入 `evaluation/reports/release/<spec>@<version>.json`。
- 外部 MCP 工具在规格中按 `mcp:<server>/<tool>` 引用；MCP 断开时，用到它的规格照常运行，只是少一个工具（现有行为不变）。

### 8.5 验收

- 测试：分桶的稳定性和比例；会话粘性；回滚后新会话立即走 stable、已有会话保持原版本（这一点在 ADR 里写明是有意为之）；自动回滚规则在合成数据上触发；歧义的规格无法启动。
- ADR 0033「Agent 规格版本化、灰度与回滚」。

---

## WP9（P1，紧跟 WP2）成功率波动归因

**原则**：成功率波动先拆成“流量构成变了”（mix）和“某个切片本身变差了”（rate）两部分，再用变更时间线对齐，最后读样本确认。Trace 回答“这一轮发生了什么”，Token 回答“模型的行为是不是变了”（轮数、输入长度、缓存命中），工具返回码回答“是不是下游坏了”，变更记录回答“谁动了什么”。

### 9.1 `scripts/attribute_success_drop.py`

参数：`--baseline 2026-11-01T00:00/2026-11-08T00:00 --current 2026-11-08T00:00/2026-11-09T00:00 [--tenant] [--channel]`

1. 整体：两个窗口的成功率、样本量、Wilson 区间（成功定义用 `turn_outcome`）。
2. 切片分解：维度包括 `agent_version`、`model`、`route_reason`、`intent_group`、`intent_source`、`channel`、`tenant`、`policy_version`、`kb_version`、`skill_fp`、`prompt_fp`、`tools_fp`、轮数分桶、input_tokens 分桶。对每个维度的每个取值 i：
   - `rate_i = s1_i × (f1_i − f0_i)`（切片自身变差）
   - `mix_i = f0_i × (s1_i − s0_i)`（流量构成变化）

   其中 s 是该切片的流量占比，f 是失败率。按 |rate_i| 降序输出 Top 10，并给出样本数。
3. 工具：按 (tool, error_class) 统计调用失败率的变化，Top 10；同时列出这个时段内的熔断打开事件（来自 `change_events` 或告警历史）。
4. Token：按切片统计 `model_requests`、`input_tokens` 的中位数和 `cache_read/input` 的变化。轮数上升往往对应 round_limit 失败，缓存命中下降往往对应 prompt 或工具定义的变更。
5. 时间线：按小时统计失败率，叠加 `change_events`。对每个变更事件比较前后 2 小时的失败率，用双比例检验标出显著的跳变。
6. 样本：对 Top 3 切片各取 5 个失败的 `request_id`，附上 trace 链接 `/trace/{id}`；如果有 badcase 快照，附快照 ID。

输出 Markdown 到 stdout，JSON 写入 `evaluation/reports/attribution_<ts>.json`。

### 9.2 Runbook

新增 `docs/runbook-success-drop.md`：

1. 看告警 → 2. 跑归因脚本 → 3. 先看变更时间线 → 4. 看 Top 切片 → 5. 读样本 → 6. 判断是版本问题（回滚，WP8）、下游问题（看熔断和适配器）、流量构成变化（不是故障），还是模型服务问题（路由切换，WP5）→ 7. 结论回填到 badcase 标注。

### 9.3 验收

- 测试：合成 `turn_records`，在 `model=X` 并且某个 `change_events(kind=model_route)` 之后的切片里注入失败率上升，其他切片不变 → 脚本把该切片排第一、把该变更标为显著。再构造一个纯 mix 的场景（只增加天然失败率较高的意图组的流量）→ 结论是 mix 而不是 rate。
- ADR 0034「成功率波动归因方法」。

---

## 2. 顺序与依赖

| 顺序 | WP | 依赖 | 估计 |
|---|---|---|---|
| 1 | WP1 资源串行化与重叠授权 | 无 | 3 天 |
| 2 | WP2 回合记录、指纹、变更事件 | 无 | 3 天 |
| 3 | WP9 归因 | WP2 | 2 天 |
| 4 | WP4 Badcase 闭环 | WP2 | 5 天 |
| 5 | WP7 记忆治理 | 无（可与 3、4 并行） | 4 天 |
| 6 | WP5 模型路由 | WP2 | 4 天 |
| 7 | WP6 P99 与成本 | WP2、WP5 | 3 天 + 评测 |
| 8 | WP3 漂移巡检与故障评测 | WP1 | 3 天 |
| 9 | WP8 规格、灰度、回滚 | WP2、WP4（门禁）、WP5 | 5 天 |

## 3. 明确不做

- 不拆成多 Agent，不引入 LLM 规划器（已有评测结论）。
- 不做模型路由的在线学习或 bandit（没有真实流量）。
- 不引入 Temporal、Kafka 或独立的向量库（现有 outbox + PostgreSQL 足够，理由见 `docs/design-tradeoffs.md`）。
- 不把聊天回执迁到数据库（见 WP3.5）。
- 不宣称跨系统 exactly-once。README 现有的边界表述保持不变。
- 不在没有跑评测的情况下改 README 里的任何数字。
