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
