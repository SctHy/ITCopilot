# IT-Copilot 开发约定

## 模块边界

| 路径 | 职责 |
|---|---|
| src/it_copilot/cmd | 分别启动 API、Worker 和隔离的验收负载 |
| api / agent | API 校验身份、请求和协议。Agent 负责知识召回、模型交互和提案，出站传输经过 agent/masking |
| workflow | 审批、状态转移、动作领取、结果提交、适配器与对账 |
| infra | 存储、认证、租户上下文、事件流、Prometheus 指标与跟踪 |
| config / migrations | 当前策略、路由和追加式迁移 |
| frontend / skills | 浏览器协议消费者与服务台业务技能 |

## 核心不变量

- 只信任认证身份和服务端租户上下文。确认、审批、人工处理均校验权限并留下审计记录。模型和检索文档无权改变授权规则。
- 决策绑定冻结的参数哈希。审批与动作入队同事务；通知只是唤醒信号，队列和未来到期动作由数据库持久化。
- 每次状态变更检查租约和版本。同一资源的外部写串行执行。效果未知或租约过期先回查，无法确定时转人工核对。
- 权限回收考虑重叠授权、原有权限和外部漂移。提交执行结果时同步更新台账和后续任务。
- SSE 请求绑定调用者、请求 ID 和内容指纹。断线只结束订阅。保留流游标语义，禁止跨租户复用结果。
- 出站脱敏覆盖流式和普通请求。跨进程传播 traceparent，指标标签保持低基数，日志避免记录密钥与原始模型内容。

## 修改与验证

先看工作区和调用链。复用已有仓储、状态机与适配器，保持 API、Worker 的独立生命周期。数据库结构通过新迁移演进。修复并发或恢复问题时保留能触发该条件的回归测试。集成验收使用隔离数据库、Redis 和测试身份。

```sh
uv sync --locked
make lint test frontend-check
make bench BENCH_ARGS=--json
.venv/bin/python scripts/dev.py check-links
docker compose config -q
docker compose --profile oidc config -q
```

API 和 Worker 的健康探针与指标通过 /livez、/readyz、/metrics 提供，告警由外部监控处理。

变更 PostgreSQL 领取逻辑时配置 IT_COPILOT_TEST_DATABASE_URL，运行 `scripts/dev.py check --profile postgres`。变更 OIDC 或目标适配器时按 CI 的 Keycloak、Gitea 配置运行 integration profile。交付说明写清实际命令、后端类型及跳过项。[基准口径](docs/benchmarks.md)
