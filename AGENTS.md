# ServiceDesk Agent 开发约定

## 模块划分

| 路径 | 职责 |
|---|---|
| `src/servicedesk_agent/cmd` | 分别启动 API、Worker 和性能/故障验收工具 |
| `api` / `agent` | `api` 负责身份认证、参数校验和接口路由；`agent` 负责知识库检索、模型交互和变更起草，发往模型的数据统一经过 `agent/masking` 脱敏 |
| `workflow` | 审批流转、状态机、任务领取、外部系统执行与定时核对 |
| `infra` | 数据库、身份认证、租户隔离、Redis 事件流、Prometheus 指标与链路追踪 |
| `config` / `migrations` | 权限策略、模型路由配置以及数据库迁移脚本 |
| `frontend` / `skills` | 前端界面与 IT 服务台业务技能说明 |

## 核心开发规则

- **权限与身份由代码控制**：只信任经过校验的登录身份和服务端租户信息；确认、审批和人工处理都必须校验权限并写审计日志，不能让模型或知识库内容绕过权限规则。
- **审批与任务同事务提交**：审批决定绑定冻结后的参数哈希；审批通过和待执行任务在同一个数据库事务里写入；PostgreSQL `LISTEN/NOTIFY` 只负责及时唤醒，任务队列和未来到期的回收任务由数据库持久化保存。
- **并发安全与先查后试**：每次更新任务状态都校验租约和乐观锁版本号；同一资源的外部写操作串行执行；遇到结果未知或租约过期接管时，先查外部系统真实状态，确认不了就转人工核对。
- **权限回收兼顾重叠授权**：定时回收权限时要结合该用户其余有效授权、原始基线权限和外部实际状态重新计算目标权限，并在提交结果时同步更新权限记录。
- **SSE 防重与断线续传**：流式请求绑定调用者、`client_request_id` 和消息内容指纹；前端点停止只关闭当前订阅，不影响后台继续执行；保持 `Last-Event-ID` 续传语义，禁止跨租户复用结果。
- **数据脱敏与安全观测**：发往模型的普通请求和流式请求均需经过脱敏；跨进程透传 W3C `traceparent` 链路 ID；Prometheus 指标只用固定枚举标签（防止标签数量失控），日志禁止输出密钥和原始敏感文本。

## 开发与验证流程

修改前先确认当前工作区和调用关系，尽量复用现有仓储、状态机和适配器，保持 API 与 Worker 两个进程的独立生命周期。表结构变更统一追加新的迁移脚本，不修改已发布的旧迁移。修复并发或故障恢复 bug 时补充对应的回归测试；集成测试使用独立的测试库、Redis 和测试身份。

```sh
uv sync --locked
make lint test frontend-check
make bench BENCH_ARGS=--json
.venv/bin/python scripts/dev.py check-links
docker compose config -q
docker compose --profile oidc config -q
```

API 和 Worker 的健康检查与监控指标通过 `/livez`、`/readyz`、`/metrics` 对外暴露，告警规则交给外部监控系统（如 Prometheus / Alertmanager）配置。

改动 PostgreSQL 任务领取逻辑时，配置 `SERVICEDESK_AGENT_TEST_DATABASE_URL` 并运行 `scripts/dev.py check --profile postgres`。改动 OIDC 认证或外部适配器时，参照 CI 配置运行 `integration` 测试。提交说明写清楚实际跑过的命令、数据库类型以及跳过项，详见[性能与故障测试报告](docs/benchmarks.md)。
