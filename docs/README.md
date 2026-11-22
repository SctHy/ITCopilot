# 架构与数据迁移

FastAPI 的 `create_app()` 为每个应用创建一个 `AppContainer`。请求依赖通过 `request.app.state.container` 取得服务，lifespan 负责启动和释放资源。对话走单 Agent 循环，知识检索融合 BM25 和向量结果。模型请求在 HTTP 传输层脱敏。

审批从申请人确认到独立审批都由服务端执行。决策校验冻结参数哈希，并在同一事务中落审批结果和待执行动作。Worker 使用租约、版本 CAS 和资源键处理竞争。外部写结果不确定时先观察目标状态，再决定是否重试。限时授权到期进入回收流程，对账器检测人工变更。

Redis Streams 保存带序号的事件。重连携带 `Last-Event-ID`。服务端校验调用方与请求指纹，拒绝复用其他身份或其他请求正文的幂等键。

## 数据库升级

Alembic `0015` 删除共享事项、IM 收发队列、质量运营数据，并将登录身份目录从 `task_identities` 重命名为 `login_identities`，供会话吊销按租户定位用户。已有部署应先备份业务数据库。早于 0014 的旧库请保留原处，本地开发可在 .env 中将 IT_COPILOT_DATABASE_URL 指向新的 SQLite 文件。降级可以重建已删除表的结构，历史行需要从升级前的备份恢复。

[审批与执行](adr/0003-transactional-outbox-worker.md) · [外部写恢复](adr/0004-ambiguous-outcomes-reconcile.md) · [事件流](adr/0005-live-event-stream.md) · [脱敏](adr/0039-model-outbound-masking.md) · [决策记录](adr/README.md)
