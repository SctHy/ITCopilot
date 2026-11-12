# 0028 周期对账与卡住工作告警

日期：2026-11-12 · 状态：已采纳

## 背景

授权只在到期回收时检查漂移，跨天期间的外部更改无法及时发现。结果未知、人工复核及已到期排队的动作需要单独告警，避免被对话可用性指标掩盖。权限之外的业务目标也需要独立读取台账验收。

## 决定

- worker 每 900 秒运行只读 `GrantReconciler`，`IT_COPILOT_RECONCILE_INTERVAL_S` 必须是正的有限数。按租户、每批最多 50 条 ACTIVE 授权读取目标状态，与 ADR 0026 的期望等级比较。
- 巡检不调用 apply。发现漂移时将该资源的有效授权标为 DRIFTED、取消对应待执行回收并审计 `grant_drift_detected`。外部临时或未知错误仅跳过当前记录并计数；其他异常抛出。自身正在执行、结果未知或已有到期工作时先让 worker 处理，避免把本服务尚未结算的效果当成外部漂移。外部读取期间台账变化也放弃本次判断。
- 漂移审计是共享事项事件的持久来源：后续巡检通过 `TaskStore.ingest` 重放通知，来源游标去重；即使标记漂移后进程退出也能补发。
- worker 每轮上报 `it_copilot_workflow_oldest_age_seconds{state}`；监控器读同一数据库计算 UNKNOWN/MANUAL_REVIEW 最久停留时间，超过 1 小时 warning、24 小时 critical；已到期 QUEUED 超过 10 分钟 warning。队列等待从 `not_before` 计算。
- 组合根注入权限、设备锁定和工单打开三个核验器。设备与工单只能读取申请人所在租户、属于申请人的台账。缺失设备或数据库不可用为 unknown，未锁定设备、缺失或关闭工单为 failed。迁移 0010 为本地工单增加状态，旧行一次性回填为 open。
- 聊天回执继续保留在 Redis，24 小时 TTL 覆盖客户端重试；工作记忆与回执维持同一个 Redis 事务。跨天审批、动作、事项和飞书收件箱已在数据库持久化，不迁移回执，不引入外部工作流引擎。

## 后果

巡检只发现问题并停止自动回收，人工决定修复目标系统。真实适配器的网络可用性、外部系统幂等能力和操作权限仍决定生产恢复边界；租约不能撤回已发出的请求，不宣称跨系统 exactly-once。

## 验证

`tests/test_grant_reconciler.py` 覆盖漂移、通知补发与去重、并发巡检、临时异常、意外异常传播、周期、配置、老化告警及两类核验的 passed/failed/unknown；同一组测试在 SQLite 与 PostgreSQL 上运行。

`scripts/evaluate_long_tasks.py` 扩展了真实子进程 SIGKILL：授权 apply 返回后未结算、批量第 2 项生效未结算、回收生效未结算，各重复 3 次。使用本地适配器和逻辑租约时钟，检查每个动作 apply 调用恰好一次、重启先回查、权限等于剩余台账期望。报告包含 9 次 worker 故障与原有 15 次事项评测，24/24 通过。这是基础设施确定性故障测试，不是模型能力、生产成功率或真实模型成本数据。

复现命令：`IT_COPILOT_BUILD_SHA=<当前提交> .venv/bin/python scripts/evaluate_long_tasks.py --repeats 3 --output evaluation/reports/long_tasks_worker_kill_2026-11-12.json`。报告见 `evaluation/reports/long_tasks_worker_kill_2026-11-12.json`；无需模型服务或生产数据。
