# 0022 多租户：租户注册表、开关与跨租户巡检

日期：2026-11-12 · 状态：已采纳

## 背景

租户此前只是令牌里的一个 claim（缺失时回落为 `default`）。审批、事项、身份目录、吊销与管理审计按它过滤，但任何发得出令牌的 realm 都能声明任意租户，部署里没有"本服务服务哪些租户"的记录，也无法停用一个租户。隔离全靠每条查询手写 `tenant ==`，没有手段发现漏写的地方。评审还发现知识库只按部门过滤：别的租户的管理员能列出、检索、**删除**本租户的文档。

升级路线见 [docs/multi-tenant-plan.md](../multi-tenant-plan.md)；本记录是阶段 0。

## 决定

- **形态**：共享部署，数据库强制隔离（PostgreSQL RLS，阶段 1），大租户以后可迁到独立的单元（cell）。SQLite 只用于单租户开发：开关打开且数据库为 SQLite 时，API 与独立 worker 拒绝启动。
- **开关** `IT_COPILOT_MULTI_TENANT`，默认关闭；关闭时不查注册表，行为与之前一致。
- **注册表** `tenants`（迁移 `0003`）：`id, status, tier, issuer, feishu_tenant_key, policy_ref, quotas`。迁移登记 `default` 和所有已有数据的租户（全局审计 `*` 不算租户），都设为 `active`，所以之后打开开关不会把原本能登录的人挡在外面。`issuer`、`feishu_tenant_key`、`policy_ref`、`quotas` 留给后续阶段使用。
- **入口校验**：开关打开时，`current_principal` 在验签之后、吊销检查之前校验租户。租户未登记返回 401（不是我们签发、也不该受理的令牌）；`suspended`/`offboarding` 返回 403；查询失败返回 503（fail closed，同 ADR 0019）。飞书消息没有令牌，`_channel_turn` 对停用租户直接回复说明，不启动模型。每个进程缓存查询结果 5 秒（`IT_COPILOT_TENANT_CACHE_S`）。
- **管理入口**：只有平台运维的 `scripts/tenants.py`（list/add/suspend/activate/offboard），没有 HTTP 路由：租户管理员不应该能开通或停用租户。变更与 `admin_audit` 记录（记在该租户名下）在同一事务中写入。租户 id 只允许 `[a-z0-9_-]`，`*` 和以 `_` 开头的 id 保留给平台。
- **跨租户巡检**（`tests/test_tenant_isolation.py`）：租户 A 准备一套带标记的数据，租户 B 的 IT 管理员、审计员、审批人，以及和 A 的申请人**同一 subject** 的员工，逐个调用所有路由。对象路由必须返回 403/404，列表路由不能带出标记，探测结束后 A 的数据不变；请求体写错导致的 422 也算失败，因为它什么都证明不了。OpenAPI 中每个（方法, 路径）都必须归入"探测 / 只作用于本租户 / 已有测试覆盖 / 平台级 / 已知缺口"之一，新路由归类之前测试不通过。已知缺口按严格 xfail 运行，修好后必须移到探测清单。
- **静态守卫**（`tests/test_tenant_query_guard.py`）：解析应用代码，找出对带 tenant 列的表做查询、却看不到租户条件（`tenant` 或包含它的共享过滤器）的函数。合理的例外列在白名单里并写明理由；新增例外或白名单过期都会让测试失败。RLS 落地之前，这是"漏写 `tenant ==`"的唯一防线。

## 后果

- 巡检确认任务、审批、历史、审计、吊销这些路由都已按租户隔离。做变异测试时，去掉 `_shared_scope` 的租户检查后，巡检立刻发现别的租户的管理员能**批准**本租户的审批单。
- 已知缺口，由后续阶段关闭：知识库列出、检索、删除（阶段 2），沙箱目录与授权台账 `/business/employees/{id}`（阶段 1）。登记为平台级、需要后续收紧的有：`/knowledge/add`、`/knowledge/upload` 写入全局库，`/knowledge/stats` 无鉴权，`/skills/reload`、`/policy/revalidate`、`/ops/retention/run` 由任一租户的管理员触发、作用于全局（阶段 1、3），`/metrics`、`/monitor` 是全局计数（阶段 5）。
- 签发方与租户尚未绑定（阶段 1）：开关打开后，可信 realm 仍可声明任何已登记的租户。在单 IdP、单组织部署下这与之前相同；接入多个 realm 之前必须完成阶段 1。
- 停用租户不取消已经在执行的工作（审批通过后的动作、到期回收）；worker 按租户停止领取工作在阶段 1 实现，那时 `actions` 才有 tenant 列。
