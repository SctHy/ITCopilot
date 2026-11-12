# 多租户升级计划

目标：把当前"预留 tenant 维度的单租户"升级为**共享部署（pooled）+ 数据库强制隔离**的多租户平台，并为大租户保留独立部署（silo/cell）的路径。

原则：

- **租户只来自可信身份**：延续 ADR 0002，任何层都不从请求体、模型输出或知识库文本里拿租户。
- **隔离靠两层兜底**：应用层显式过滤（已有），再加数据库 RLS；任何一层漏掉都不泄漏。
- **故障域按租户切分**：模型配额、工具熔断、执行队列、可观测性都按租户划分，一个租户出问题不拖垮别的租户。
- **跨租户访问返回 404 而不是 403**，不暴露对象是否存在。
- 全程放在开关 `IT_COPILOT_MULTI_TENANT` 后面；关闭时行为与现在完全一致，已有数据全部回填到 `default`。

## 现状差距（摘自评审）

| 层 | 现状 | 差距 |
|---|---|---|
| 身份 | tenant 取自 JWT claim，缺失时默认 `default`（`core/identity.py:196`） | 缺失时不应回落；签发方（issuer）与租户没有绑定 |
| 状态 | `service_tasks`/`approvals`/`admin_audit`/`session_revocations`/`task_identities` 有 tenant 列；查询手写过滤 | `actions`/`access_grants`/`audit`/`approval_decisions`/`task_events` 等没有 tenant 列；没有 RLS |
| 知识库 | 只按 `department` 过滤，`"*"` 全局可见（`tooling/knowledge_base.py:240`） | **跨租户泄漏** |
| 策略 | 一份全局 `access_policy.yaml`，变更记在 `GLOBAL_TENANT="*"` 下 | 不能按租户定制；一次改动会复核所有租户的在途工作 |
| 模型 | 一个全局 client/key；限流按人（`scope_key`） | 没有租户配额、预算和并发舱壁 |
| 工具 | 熔断按工具名划分；适配器只有一套全局凭据 | 租户之间互相熔断；一份凭据的爆炸半径覆盖所有租户 |
| 执行 | 一个 outbox，worker 按租约领取 | 没有租户公平调度 |
| 可观测 | span 只带 request_id；指标没有 tenant 标签；`/metrics` 不鉴权；`trace:recent` 是全局列表 | 没法按租户算 SLO、排障或计费 |

## 阶段划分

P0 是安全底线，上线多租户前必须完成；P1 处理故障域；P2 处理运营和规模。每个阶段都能单独合入、单独回滚。

### 阶段 0 · 决策与测试脚手架（P0，约 2 天）

> **状态：已完成**（ADR 0022）。代码：`core/tenancy.py`、迁移 `0003`、`scripts/tenants.py`、`tests/test_tenant_isolation.py`、`tests/test_tenant_query_guard.py`。
> 与原计划的差异：worker 侧的租户状态检查移到阶段 1，因为 `actions` 要到那时才有 tenant 列。巡检还带出以下需要后续处理的事项：
> - 阶段 1：`/business/employees/{id}`（沙箱目录与授权台账）会跨租户泄漏；`/ops/retention/run` 由任一租户的管理员触发、作用于全局。
> - 阶段 2：工具结果缓存（`MCPToolManager` 的 `cache_ttl`）的 key 由参数决定，租户由代码注入后必须进入缓存 key，否则不同租户会命中彼此的缓存结果；`/knowledge/stats` 没有鉴权。
> - 阶段 3：`/skills/reload`、`/policy/revalidate` 是部署级操作，应改为平台管理员角色，或者只作用于本租户。

1. **ADR 0022 租户模型**：采用共享部署 + PostgreSQL RLS；SQLite 只用于单租户开发，多租户开关打开且数据库为 SQLite 时启动直接失败。
2. **租户注册表** `tenants`：`id, status(active/suspended/offboarding), tier, issuer, feishu_tenant_key, policy_ref, quotas(JSON), created_at`。所有请求和 worker 动作在入口都先校验租户 `active`。
3. **双租户测试夹具**：`tests/conftest.py` 增加 `tenant_a`/`tenant_b` 两套身份和数据。新增 `tests/test_tenant_isolation.py`：遍历 FastAPI 的所有路由，用 B 的 token 访问 A 的对象，期望 404 或空列表。之后每个阶段都往里面加用例。
4. **静态守卫**：写一个测试，扫描 `select(...)`/`update(...)` 里引用了租户作用域表的语句，要求带上 tenant 条件或经过统一的 `tenant_scope()` 辅助函数。在 RLS 落地前先挡住"漏写"。

### 阶段 1 · 身份与状态隔离（P0，约 1 周）

> **状态：代码已完成，PostgreSQL 验证待做**（ADR 0023）。与原计划的差异：
> - `task_*` 子表不加 tenant 列，RLS 策略跟随父表 `service_tasks`；只有会脱离父表单独读取的表才加列（审批子表、沙箱目录）。
> - RLS 只保护请求路径；后台循环运行在显式的系统作用域 `*`，不逐个租户执行。
> - 默认 compose 仍以超级用户连接（超级用户会绕过 RLS）；多租户模式要求切换到 `deploy/postgres/app-role.sql` 创建的 `itc_app`，否则拒绝启动。
> - 待办：在 PostgreSQL 上用 `itc_app` 跑完整测试套件和 `tests/test_row_level_security.py`（开发环境连不上本地数据库）；跨租户访问统一返回 404；`/ops/retention/run` 移到阶段 3，与其他部署级操作一起处理。

**身份**

- `core/identity.py`：多租户模式下 tenant claim 缺失就返回 401，不再回落到 `default`。
- 绑定签发方与租户：`tenants.issuer` 必须等于 token 的 `iss`（Keycloak 每个租户一个 realm，或者用 Organization claim 加白名单）。这样一个 realm 的管理员没法签发别的租户的 token。
- 飞书入口（`intake/im.py`）：用事件里的 `tenant_key` 映射到 `tenants.feishu_tenant_key`，映射不到就丢弃；每个租户使用自己的应用凭据。

**数据库（迁移 `0003_tenant_columns_and_rls`）**

1. 给 `actions`、`access_grants`、`audit`、`approval_decisions`、`task_events`、`task_runs`、`directory_employees`、`vector_rows`，以及 `core/task_extensions.py` / `task_product_schema.py` 里的任务扩展表加 `tenant`。按 `approval_id` / `task_id` 从父表回填，然后设为 `NOT NULL`。
2. 索引和唯一约束改成以 tenant 开头，例如 `(tenant, approval_id, id)`、`(tenant, collection, id)`。审计的"只追加"触发器（0002）保持不变。
3. 开启 RLS：每张表 `ENABLE` + `FORCE ROW LEVEL SECURITY`，策略为 `tenant = current_setting('app.tenant', true)`。
4. 拆分数据库角色：应用角色 `itc_app` 不是表的所有者，也没有 `BYPASSRLS`；迁移用 `itc_owner`。
5. 设置会话变量：在 `core/db.py` 注册 SQLAlchemy 的 `begin` 事件，从 `contextvars` 读取当前租户并执行 `SET LOCAL app.tenant = :t`；租户未设置时变量为空，按策略一行都看不到，即默认拒绝。
6. 跨租户的后台任务（保留清理、到期回收、策略复核、审批过期）改成"逐个租户在 `with tenant_context(t):` 里执行"，不用绕过 RLS 的角色。

**Redis 与事件流**

- 所有 key 加上 `t:{tenant}:` 前缀：`trace:recent` 改为每个租户一个列表，限流、锁（`core/concurrency.py`）、事件流（`core/events.py`）也一样。
- 记忆模块已经使用 `scope_key`，只需补一个回归测试。

**验收**

- 双租户测试套件全部通过。
- 在 `itc_app` 角色下执行不带 WHERE 的 `SELECT * FROM approvals`，只返回当前租户的行。
- 开关关闭时，现有测试套件全部通过。

### 阶段 2 · 知识库与检索（P0，约 3 天）

> **状态：已完成**（ADR 0024）。与原计划的差异：按租户分 collection（命名空间），不加 tenant 列：各租户的检索和 BM25 统计互不影响，`default` 的数据不用迁移；代价是 `vector_documents` 没有 RLS。平台公共库 `_platform` 与订阅暂未实现。工具缓存 key 已包含租户；`/knowledge/stats` 改为需要登录；多租户模式下 `/mcp` 的检索直接报错。

- `vector_rows` 带 tenant（阶段 1 已加列）。`KnowledgeBase.search` 的租户**从工具上下文里的 principal 注入**，不出现在工具参数 schema 里，模型改不了。
- 语义调整：`department="*"` 改为"本租户内全员可见"；另设一个只读的平台公共库（tenant=`_platform`），租户需要显式订阅才能检索到。
- 本地缓存（BM25 索引、`_department_mask`）的 key 改为 `(tenant, department)`；文档上传、列出、删除等管理接口按租户划分作用域，写入 `admin_audit` 时记在本租户名下。
- 情景记忆和画像向量（`memory/conversation_memory.py`）同样要求按 tenant 过滤。

**验收**：在 A 的知识库放一篇带唯一标记的文档，B 用这个标记检索，结果为 0；新增一组安全评测用例："A 的文档里注入指令，诱导读取 B 的数据"，加入 `scripts/evaluate_security.py`。

### 阶段 3 · 策略与工具（P1，约 1 周）

> **状态：已完成**（ADR 0025）。与原计划的差异：
> - 覆盖文件以租户 id 命名，不使用注册表里的 `policy_ref` 字段；第一次加载就失败时直接报错，不退回基线。
> - 熔断不按租户拆分：工具调用的都是共享下游，参数错误本来就不计入失败；按租户区分的是执行适配器。
> - `/skills/reload`、`/policy/revalidate`、`/ops/retention/run` 都在本阶段处理：多租户模式下需要运营方租户的 `platform_admin`；租户管理员只能复核本租户。
> - 未实现：外部 MCP 服务按租户设置白名单。

**按租户覆盖策略（ADR 0023）**

- 目录结构：`config/policies/base.yaml` 是平台基线，`config/policies/tenants/<tenant>.yaml` 是租户覆盖。
- 合并规则**只能收紧**：审批人数只能增加、`max_hours` 只能缩短、可申请的系统和角色只能取子集、`on_behalf_roles` 只能更少。加载时校验，违反就拒绝加载，并记录一条 `failed` 审计。
- 每个租户单独计算版本和指纹，审批快照记录 `"<tenant>:<version>+<fingerprint>"`。
- 复核按租户进行（`workflow/policy.py` 中的 revalidate）：改一个租户的覆盖文件，只复核该租户；改基线，所有租户都复核。
- 策略事件从 `GLOBAL_TENANT` 改为记在本租户名下；基线变更时给每个租户各写一条审计。

**工具与适配器**

- 凭据按租户隔离：`tenants.adapters` 里只存 secret 引用（vault 路径或环境变量名），不存明文。
- `workflow/worker.py:310` 的适配器构造改为工厂 `adapter_for(tenant, system)`，结果按 `(tenant, system)` 缓存。worker 从 `action.tenant` 解析适配器。
- 工具可见性按租户配置：租户没开通的系统，模型拿不到对应的工具 schema，策略层也会拒绝，形成双重检查。
- MCP 服务器（`config/mcp_servers.json`）增加租户白名单。

**熔断粒度**

- 熔断键从工具名改为 `(tool, downstream)`。`downstream` 指实际调用的端点：多个租户共用同一个下游时共享熔断，这是正确的；租户各自的下游互不影响。
- 401/403/4xx 这类"租户自身配置错误"不计入熔断失败次数，只记在租户级的健康指标里，避免一个租户的错误凭据把共享下游熔断掉。

### 阶段 4 · 模型与资源故障域（P1，约 1 周）

**配额（扩展 `core/rate_limit.py`，GCRA 本身已支持多个 bucket）**

- 每轮请求检查两层限流：`agent_turn:tenant:{t}`，再检查 `agent_turn:user:{scope_key}`。租户级速率从 `tenants.quotas` 读取，按 tier 设默认值。
- 新建 token 用量表 `tenant_usage(tenant, day, model, input_tokens, output_tokens, cost)`，在 `core/llm.py` 统一记账。超出日预算时**降级而不是报错**：只走检索和模板回答，并停止起草新的写操作申请。

**舱壁**

- 每个副本内设置租户级 `asyncio.Semaphore`，限制 LLM 并发和工具并发；再叠加一个全局上限。
- 并发超限时排队，超过等待时间返回 429。前端已经能处理限流，沿用即可。

**模型路由**

- 租户配置可以指定 `provider/model/key_ref`，支持自带 key（BYOK）。client 池按 `(provider, key_ref)` 缓存。
- 供应商故障时，按租户配置的备选模型切换；没有配置就沿用平台默认。

**执行队列公平调度**

- 修改 worker 的领取 SQL：用 `ROW_NUMBER() OVER (PARTITION BY tenant ORDER BY created_at)` 做轮转，每个租户每轮最多领 N 个，配合 `FOR UPDATE SKIP LOCKED`。
- 每个租户设在途上限（`max_inflight`），超出的动作留在队列里。
- 增加 `IT_COPILOT_WORKER_TENANTS` 环境变量：指定后 worker 只处理这些租户，可以给大租户单独开 worker 池，这也是 cell 化的第一步。

**验收（噪声邻居压测，新增 `scripts/evaluate_noisy_neighbor.py`）**：A 以 10 倍配额压测时，B 的整轮 p95 增幅不超过 20%，B 的 429 为 0，B 的审批执行延迟不超过基线的 1.5 倍。

### 阶段 5 · 日志、审计与可观测（P1，约 3 天）

- **Trace**：`monitor/telemetry.py` 在根 span 上设置 `it_copilot.tenant`，并通过 OTel baggage 传给子 span。
- **指标**：加 `tenant` 标签，但要控制基数：tier 为 enterprise 的租户用真实 id，其余归为 `tier:<name>`。SLO 燃烧率按租户计算。
- `/metrics` 只在内网端口暴露，或要求 `ops` 角色。
- **日志**：使用结构化 JSON，固定包含 `tenant`、`request_id`、`actor` 字段；`core/redaction.py` 的脱敏规则保持不变。
- **审计**：新增 `GET /admin/audit/export`，按租户导出 JSONL 并附带哈希链摘要；保留期（`core/retention.py`）改为可以按租户配置。

### 阶段 6 · 租户生命周期与单元化（P2，按需）

- **开通**：一个脚本完成注册表记录、Keycloak realm 或 org、策略覆盖文件、空知识库、默认配额，并写入审计。
- **暂停**：`status=suspended` 后，拒绝该租户的新 token（复用 0019 的吊销水位线，写入租户级水位），worker 跳过该租户的动作，进行中的授权照常到期回收。
- **下线**：导出 → 回收所有 `access_grants` → 按 tenant 删除数据（RLS 下就是一条 `DELETE`）→ 删除凭据，审计留存到法定期限。
- **单元化（silo）**：增加 `tenant → database_url / redis_url / worker 池` 的路由表，大租户迁到独立的 cell。应用代码不需要改动，因为租户已经贯穿所有层。

## 迁移与回滚

1. 迁移 0003 只加列、回填、加索引，不改行为。RLS 策略单独放在 0004，可以先在预发环境开启观察。
2. 先打开开关 `IT_COPILOT_MULTI_TENANT=false`，以单租户方式运行，确认 RLS 下 `default` 租户的行为不变。
3. 在预发环境创建第二个租户，跑双租户测试套件、安全评测和噪声邻居压测。
4. 生产环境打开开关。回滚时关闭开关；RLS 策略可以单独 `DISABLE`，数据不需要回迁。

## 默认取舍（未另行说明时采用）

| 问题 | 默认选择 | 理由 |
|---|---|---|
| 隔离形态 | 共享表 + RLS，大租户走单元化 | 成本最低；RLS 提供数据库级兜底 |
| IdP | Keycloak 每个租户一个 realm，`iss` 绑定租户 | 管理员权限天然隔离，伪造 claim 无效 |
| SQLite | 只支持单租户开发 | SQLite 没有 RLS，强行支持只会留下隐患 |
| 平台公共知识 | `_platform` 租户 + 显式订阅 | 避免"`*` 等于全局"的语义再次泄漏 |
| 超出预算 | 降级为只读回答 | 保证可用；写操作本来就要审批，不急于这一轮 |

## 工作量与顺序

| 阶段 | 优先级 | 估计 | 依赖 |
|---|---|---|---|
| 0 决策与测试脚手架 | P0 | 2 天 | — |
| 1 身份与状态隔离 | P0 | 1 周 | 0 |
| 2 知识库 | P0 | 3 天 | 1 |
| 3 策略与工具 | P1 | 1 周 | 1 |
| 4 模型与资源故障域 | P1 | 1 周 | 1（可与 3 并行） |
| 5 可观测 | P1 | 3 天 | 1 |
| 6 生命周期与单元化 | P2 | 按需 | 3、4 |

P0（约 2.5 周）完成后，系统就**可以安全地承载多个租户**；P1 完成后，系统才**能扛住噪声邻居**。
