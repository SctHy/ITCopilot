# 架构决策记录（ADR）

每条记录一个现行的决定：背景、决定、后果。被推翻的决定直接删除，取代关系写在新记录里。

| 编号 | 决定 | 状态 |
|---|---|---|
| [0001](0001-llm-proposes-code-authorizes.md) | 模型只提出变更，身份、策略、审批与执行由确定性代码负责 | 已采纳 |
| [0002](0002-identity-from-verified-token.md) | 调用者身份只取自已验证令牌，请求体中的身份字段一律忽略 | 已采纳 |
| [0003](0003-transactional-outbox-worker.md) | 审批通过与执行动作同事务写入，由租约 worker 执行 | 已采纳 |
| [0004](0004-ambiguous-outcomes-reconcile.md) | 外部调用三分类；结果未知先回查再决定是否重做 | 已采纳 |
| [0005](0005-live-event-stream.md) | 执行事件写入持久流，SSE 实时订阅并可续传 | 已采纳 |
| [0007](0007-supersede-pending-approvals.md) | 同一会话的新申请替代仍待审批的旧申请 | 已采纳 |
| [0008](0008-revalidate-on-policy-change.md) | 策略变更后按新版本复核在途审批与授权，只收紧不放宽 | 已采纳 |
| [0009](0009-feishu-channel-stored-work.md) | 飞书入口：先落库再处理，只在被叫到时说话 | 已采纳 |
| [0012](0012-shared-service-tasks.md) | 三方共享事项：事件持久化、按事项串行、审批结果唤醒只读跟进 | 已采纳 |
| [0013](0013-task-capabilities.md) | 事项进度、协作待办、定时提醒与运行治理 | 已采纳 |
| [0014](0014-task-product-capabilities.md) | 共享事项的协作管理与独立通知 | 已采纳 |
| [0015](0015-task-execution-verification.md) | 步骤检查点、独立目标核验与有限重规划 | 已采纳 |
| [0016](0016-identity-scope-and-task-history.md) | 租户内的用户隔离、显式事项成员与历史流程续接（取代 0012、0014 的默认部门访问规则） | 已采纳 |
| [0017](0017-requester-withdraw-and-early-release.md) | 申请人撤回待审批申请与提前归还限时权限 | 已采纳 |
| [0018](0018-rate-limit-and-retention.md) | 按身份限流与数据保留清理 | 已采纳 |
| [0019](0019-token-revocation-watermark.md) | 按身份的令牌吊销水位线 | 已采纳 |
| [0020](0020-requester-confirms-drafted-requests.md) | 申请人确认 Agent 起草的申请后才进入审批 | 已采纳 |
| [0021](0021-audit-integrity-and-policy-identity.md) | 审计完整性：来源摘要、管理操作先写意图、审计表只追加、策略按内容识别（修订 0008） | 已采纳 |
| [0022](0022-multi-tenant-registry-and-isolation-sweep.md) | 多租户阶段 0：租户注册表与开关、跨租户路由巡检、未限定租户的查询静态守卫 | 已采纳 |
| [0023](0023-tenant-scope-and-row-level-security.md) | 多租户阶段 1：租户作用域、签发方与飞书工作区绑定、审批子表与沙箱目录带 tenant、PostgreSQL 行级安全 | 已采纳 |
| [0024](0024-per-tenant-knowledge-libraries.md) | 多租户阶段 2：每个租户一个知识库，检索时由代码注入租户，工具缓存 key 包含租户 | 已采纳 |
| [0025](0025-tenant-policy-overlays-adapters-and-platform-admin.md) | 多租户阶段 3：只能收紧的租户策略覆盖、租户自己的目标系统与凭据、部署级操作需要平台管理员 | 已采纳 |
| [0026](0026-resource-serialization-and-ledger-state.md) | 资源级串行化与台账期望状态 | 已采纳 |
| [0027](0027-turn-records-and-change-events.md) | 持久回合元数据、版本指纹、工具错误类别与行为变更事件 | 已采纳 |
| [0034](0034-success-drop-attribution.md) | 成功率波动的切片 rate/mix 分解与变更时间线归因 | 已采纳 |
| [0032](0032-long-term-memory-governance.md) | 长期记忆写入脱敏、准入、近重复合并及字段时效和本人删除 | 已采纳 |
| [0029](0029-online-badcase-feedback-loop.md) | 反馈、脱敏快照、双人标注与线上回归闭环 | 已采纳 |
