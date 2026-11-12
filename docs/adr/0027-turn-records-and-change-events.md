# ADR 0027：回合记录、版本指纹与变更事件

状态：已采纳

## 背景

Redis trace 只保留七天，不能据此比较部署版本、模型或技能变化后的成功率。单一工具失败桶也无法区分输入错误、权限拒绝、下游不可用。

## 决定

迁移 0007 新增 `turn_records`、`change_events`，新表启用 PostgreSQL RLS。回合只存身份作用域、版本、意图、阶段时延、模型请求与 token 数、工具名和错误类别；不存工具参数、结果或消息正文。私人对话与飞书在结算路径写入，共享事项与 `TaskStore.finish` 在同一个事务提交。request_id 唯一约束使重试重复结算幂等。

Agent 运行时构建时计算提示词、工具 Schema、技能快照的 SHA-256；技能内容变化后重建缓存。策略版本来自所属租户的有效策略，知识版本来自该租户 collection 最近写入时间。`IT_COPILOT_BUILD_SHA` 必须非空；本地启动器使用 git revision，镜像通过 BUILD_SHA 构建参数注入。

所有成功率查询共享 `agents.outcome.turn_outcome`：虚假声明未纠正、轮数耗尽和模型不可用是独立终态；partial 不算 success。业务目标是否达成仍由独立目标核验决定。

Agent 工具指标改为 `it_copilot_agent_tool_calls_total{tool,error_class}`，类别为 success/input_invalid/guard_blocked/policy_denied/timeout/circuit_open/upstream_4xx/upstream_5xx/error。工具 Schema 错误、写守卫拒绝、策略禁止分别有明确异常类型；HTTP 状态码与 MCP 治理结果保留错误分类。

部署、策略、技能、知识、MCP 工具集合变更统一写入 `record_change`；`GET /admin/changes` 仅向本租户管理员和审计员暴露所属租户和平台事件。回合默认保留 90 天，`IT_COPILOT_RETENTION_TURN_DAYS=0` 禁止清理；非法新增配置启动失败。

## 后果

Redis 工作记忆与回执仍原子提交；SQL 回合元数据与 Redis 不是分布式事务。SQL 记录先提交，Redis 写入失败时存在已记录的运行，成功终态重放不会覆盖原记录；共享事项最终失败后由人重新处理成功时，同 request_id 的失败记录可以被新权威结算更新。正文需长期保留时由后续 badcase 流程单独脱敏快照。非流式请求的 first_token_ms 为 NULL；未配置价格前 cost_usd 为 NULL，不能据此推导成本。

## 验证方法

```sh
.venv/bin/pytest -q tests/test_turn_records.py tests/test_chat_endpoint.py tests/test_observability.py tests/test_shared_tasks.py tests/test_retention.py
IT_COPILOT_TEST_DATABASE_URL=postgresql+psycopg://... .venv/bin/pytest -q tests/test_turn_records.py tests/test_shared_tasks.py tests/test_retention.py
```

测试覆盖真实 HTTP 与 FunctionModel 工具循环、技能重载指纹和变更事件、工具拒绝分类、版本必填、租户过滤、持久化字段和流式首字时延。真实模型效果评测未运行。
