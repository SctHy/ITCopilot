# 架构决策记录（ADR）

每条记录一个已采纳的决定：背景、决定、后果。被推翻时新增一条并标注取代关系，不改写旧记录。

| 编号 | 决定 | 状态 |
|---|---|---|
| [0001](0001-llm-proposes-code-authorizes.md) | 模型只提出变更，身份、策略、审批与执行由确定性代码负责 | 已采纳 |
| [0002](0002-identity-from-verified-token.md) | 调用者身份只取自已验证令牌，请求体中的身份字段一律忽略 | 已采纳 |
| [0003](0003-transactional-outbox-worker.md) | 审批通过与执行动作同事务写入，由租约 worker 执行 | 已采纳 |
| [0004](0004-ambiguous-outcomes-reconcile.md) | 外部调用三分类；结果未知先回查再决定是否重做 | 已采纳 |
| [0005](0005-live-event-stream.md) | 执行事件写入持久流，SSE 实时订阅并可续传 | 已采纳 |
| [0006](0006-default-generalist-strategy.md) | 默认使用通用 Agent；多 Agent 改为 LLM 规划、只在要求时建单 | 已采纳 |
| [0007](0007-supersede-pending-approvals.md) | 同一会话的新申请替代仍待审批的旧申请 | 已采纳 |
| [0008](0008-revalidate-on-policy-change.md) | 策略变更后按新版本复核在途审批与授权，只收紧不放宽 | 已采纳 |
| [0009](0009-feishu-channel-stored-work.md) | 飞书入口：先落库再处理，只在被叫到时说话 | 已采纳 |
| [0010](0010-proactive-intake-observe-first.md) | 告警主动建单：规则准入、先观察后自动、不绕过审批 | 已废弃 |
| [0011](0011-evaluation-gated-skill-improvement.md) | 技能自改进：失败归因、模型提案、评测门控，提案只能改技能文本 | 已采纳 |
| [0012](0012-shared-service-tasks.md) | 三方共享事项：事件持久化、按事项串行、审批结果唤醒只读跟进 | 已采纳 |

- [0013 事项进度、协作待办、定时提醒与运行治理](0013-task-capabilities.md)
- [0014 共享事项的协作管理与独立通知](0014-task-product-capabilities.md)
- [0015 步骤检查点、独立目标核验与有限重规划](0015-task-execution-verification.md)
- [0016 租户内的用户隔离、显式事项成员与历史流程续接](0016-identity-scope-and-task-history.md)（取代 0012、0014 的默认部门访问规则）
