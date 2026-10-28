# 0003 审批通过与执行动作同事务写入（outbox），由租约 worker 执行

日期：2026-10-28 · 状态：已采纳

## 背景

v2 在批准接口内同步执行本地事务。接入真实外部系统后，HTTP 请求内调用外部 API 会把可用性绑在一起；而“批准”和“排队执行”分两步写时，中间崩溃会留下“已批准却永不执行”的申请。

## 决定

- 达到审批人数的那次状态迁移，在同一事务内插入 `actions` 行（兼作 outbox）。
- worker 按 `not_before` 认领到期动作：版本号 CAS + 租约（`lease_owner`、`lease_until`）。租约过期的 `executing` 动作可被重新认领，并先回查（见 ADR 0004）。
- 同一审批的动作按序执行；第一个动作执行前对整批做 preflight，常见的部分失败（员工不存在等）在零变更时就被发现。
- 中途确定失败：取消后续动作，已授予的权限立即排期回收（saga 补偿）；不可逆变更（密码重置、建单）转 `manual_review`。
- 授权成功时在同一事务登记到期回收动作。
- 批准接口最多等待 `IT_COPILOT_APPROVAL_WAIT_S` 秒让快速路径直接返回结果，慢的外部系统返回 `queued/executing`。

## 后果

- API 内 worker 与独立 `python -m workflow.worker` 可同时运行，CAS 保证同一动作只被一个 worker 执行。
- 状态源唯一：审批与执行状态只在工作流数据库（PostgreSQL / SQLite），Redis 不再保存审批。
- 不提供跨系统 exactly-once；见 ADR 0004。
