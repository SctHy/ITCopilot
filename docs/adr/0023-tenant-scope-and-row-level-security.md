# 0023 多租户：租户作用域、签发方绑定与行级安全

日期：2026-11-12 · 状态：已采纳（补充 0022）

## 背景

阶段 0（ADR 0022）登记了租户，巡检确认请求路径的查询都带了租户条件，但这些条件都写在应用层：漏写一处就会越权，数据库没有兜底。另外：

- 任何受信任的签发方都能声明任意租户。
- 审批的子表（动作、授权台账、审计、投票）没有 tenant 列。
- 沙箱目录全局只有一份：别的租户的审批人能查本租户员工的目录和授权台账，两个租户也不能都有 `1001` 号员工。

## 决定

**租户作用域**（`core/tenant_context.py`）：用一个 contextvar 记录"当前代码为哪个租户工作"。只在入口设置一次，之后由需要的地方读取，不靠层层传参。

| 入口 | 作用域 |
|---|---|
| HTTP 请求 | `current_principal` 验签后进入调用者的租户。它改为 async：同步依赖运行在线程池里，在那里设置的 contextvar 传不到 endpoint。`asyncio.to_thread` 和新建的 task 都会复制作用域。 |
| 飞书消息 | 每一轮进入发送者的租户。用 `with` 包住，因为一个渠道循环要服务多个租户。 |
| 执行 worker | 过期、复核、领取动作在系统作用域 `*`；每个领到的动作在它自己的租户作用域里执行。 |
| 事项 worker、目标核验 | 进入所属事项的租户。 |
| 启动过程与其余后台循环 | 系统作用域 `*`。 |
| 迁移 | 系统作用域。 |

没有进入任何作用域的代码就没有作用域。`require_tenant()` 和 RLS 都会拒绝它，不会猜测。

**签发方绑定**：

- `Principal.issuer` 记录令牌的签发方。
- 多租户模式下，令牌必须带 tenant claim；签发方必须是该租户登记的 `issuer`。租户没有登记 issuer 时，用部署的默认签发方（OIDC_ISSUER，开发模式为开发签发方）。不匹配返回 401。
- OIDC 信任的签发方是 OIDC_ISSUER 加上各租户登记的 realm。先读未验签的 `iss` 来决定去哪里取公钥，不受信任的 `iss` 在取公钥之前就拒绝。
- 飞书：事件 `header.tenant_key` 必须等于发送者所在租户登记的 `feishu_tenant_key`，否则按未绑定处理。

**tenant 列**（迁移 `0004`）：

- `actions`、`access_grants`、`audit`、`approval_decisions` 从审批单复制 tenant。审计写入时用审批单的标量子查询取 tenant：当前作用域看不到这张审批单时，子查询结果为 NULL，NOT NULL 约束会拒绝写入。
- `directory_*` 的主键加上 tenant。本地适配器只操作当前作用域租户的目录。启动时只给 `default` 播种演示目录。
- `task_*` 子表不加列：它们的策略跟随父表 `service_tasks`。

**行级安全**（迁移 `0005`，只在 PostgreSQL 上生效）：

- 每个事务开始时，`core.db` 执行 `set_config('app.tenant', <作用域>, true)`。设置随事务结束而失效，连接池里的连接不会把上一个请求的租户带给下一个请求。
- 有 tenant 列的表：只有行的 tenant 等于作用域、或作用域为 `*` 时，行可见、可写。`admin_audit` 中 `*` 租户的全局行在任何作用域都可读。
- `task_*` 子表：父事项可见时，行才可见。
- `app.tenant` 未设置或为空时，什么都匹配不到。
- 所有策略都设了 `FORCE`，表的所有者也受约束。
- 不在 RLS 范围内的表：`tenants`（平台注册表）、`channel_inbox`/`channel_outbox`（入库时还不知道租户）、`vector_documents`（阶段 2 处理）。

**数据库角色**：超级用户和带 BYPASSRLS 的角色无视所有策略，设了 FORCE 也一样。官方镜像把 `POSTGRES_USER` 创建为超级用户。

- 多租户模式下，连接角色能绕过 RLS 时，API 和 worker 拒绝启动。
- 单租户模式只记录一条警告。
- `deploy/postgres/app-role.sql` 创建普通角色 `itc_app`，并把表的所有权转给它。

## 后果

- **默认 compose 仍以超级用户连接**，这是有意的。RLS 的策略已经创建，但要切换到 `itc_app` 才会生效。原因是：开发环境不能连接本地 PostgreSQL（本机套接字被系统拦截），所以完整测试套件还没有在 RLS 下跑过。`tests/test_row_level_security.py` 用普通角色验证策略本身，需要 `IT_COPILOT_TEST_DATABASE_URL` 才会运行。
- **RLS 保护的是请求路径**：调用者可控的输入都经过请求路径。后台循环运行在系统作用域，它们的租户隔离仍然依赖应用层的过滤条件，也就是静态守卫的白名单（ADR 0022）。
- 一段需要访问租户数据、却没有进入作用域的新代码路径，在 PostgreSQL + `itc_app` 下会读不到数据、写入失败，不会越权；在 SQLite 下则不受影响。上线前要在 PostgreSQL 上用 `itc_app` 跑一遍完整测试套件。
- `employee()` 必须显式传入 tenant；`username_of()` 和本地适配器读取作用域，没有作用域时报错。
- 降级迁移 `0004` 时，如果多个租户有同号员工，主键会冲突；这次降级只适用于单租户数据。
- 仍未处理：跨租户访问统一返回 404（目前有的返回 403），`/ops/retention/run` 由租户管理员触发、作用于全局（与 `/skills/reload`、`/policy/revalidate` 一起放到阶段 3，改为平台管理员角色）。
