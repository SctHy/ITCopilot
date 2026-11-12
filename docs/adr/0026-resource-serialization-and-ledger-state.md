# 0026 资源级串行化与台账期望状态

日期：2026-11-12 · 状态：已采纳

## 背景

审批内的顺序与动作 CAS 不能阻止不同审批同时操作一个账号。重叠授权若把当前权限当成永久 baseline，会把后一笔授权提前收走，或把前一笔临时权限保留为永久权限。

## 决定

- 写动作映射为租户内的资源键：权限按员工和系统、账号按员工、设备按设备；工单使用动作 ID，彼此独立。迁移 0006 回填历史动作并移除 revoke 参数中的旧 baseline。
- `executing` 动作的 `resource_key` 有部分唯一索引。worker 的领取 CAS 在保存点内执行；同资源唯一约束冲突仅跳过该候选，其他数据库错误继续抛出。租约到期仍由同一行重新领取。
- 期望等级为最早有效授权的 baseline 与全部有效授权角色的最大值。新授权继承该 baseline；只有 baseline 自身满足要求时才算 preexisting。由其他临时授权覆盖时仍记 ACTIVE、安排独立回收并记录 `covered_by`，不重复调用 apply。
- 回收在同一数据库事务读取含本笔与排除本笔的期望状态；当前权限必须匹配前者，适配器显式恢复到后者。最后一笔回收保留永久 baseline；没有永久 baseline 时删除权限。恢复目标在外部调用前持久化，未知结果先按该目标回查。saga 复用同一回收路径。
- 审批读取时展示同租户同资源的其他在途申请，不自动替代跨会话申请。
- 尚未结算的 EXECUTING、UNKNOWN 和 MANUAL_REVIEW 持续保留资源；新 QUEUED 动作等待其回查或人工通过现有 reconcile 端口恢复，避免把未入账的临时权限当成永久 baseline。SQLite 的写事务和 PostgreSQL 的资源键事务 advisory lock 串行保护领取时的未决状态检查，执行中部分唯一索引继续作为最后一道约束；候选查询预先排除被保留的资源，其他资源可继续运行。
- 有 ACTIVE 台账时，新授权执行前也要比较实际权限与台账期望。不一致就原子标记旧授权漂移、停止对应回收并将新动作交给人工，不能把外部升级或删除洗成新的临时授权。
- UNKNOWN 回收先按旧命令观察；确认未生效后重新计算最新台账目标，持久化新目标再调用适配器。旧命令已经生效但当前状态不等于最新目标时停止并保存人工核查证据，不能复用已有幂等键改目标。

## 后果

同一资源的所有已领取动作串行，多个资源仍可并行。数据库约束不会撤回已发出的外部请求，因而不提供跨系统 exactly-once。外部人员更改权限仍进入人工复核；台账之外的永久权限不能由临时授权覆盖关系推断。

## 验证

实现前新增测试确认同角色重叠错误记为 preexisting、升级重叠错误继承 reporter、不同 worker 同资源并发和缺少 conflicts 四个失败。修复后 SQLite 与 PostgreSQL 工作流、适配器及重叠授权测试通过；PostgreSQL 用独立测试容器，不涉及生产系统。

复现：`.venv/bin/pytest -q tests/test_overlapping_grants.py tests/test_workflow.py tests/test_tool_executor_and_adapters.py`；设置 `IT_COPILOT_TEST_DATABASE_URL` 后执行同一命令覆盖 PostgreSQL 契约。
