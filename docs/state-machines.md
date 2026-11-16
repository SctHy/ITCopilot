# 状态与恢复契约

业务状态以数据库、Redis 回执及外部实际状态为准。`Machine` 校验转换，`Protocol/Flow` 校验进程内阶段；二者都不能替代事务、CAS、租约和持久化。普通聊天/模型调用使用顺序控制流，不再额外建状态机。

| 流程 | 必须保持的契约 | 实现入口 |
|---|---|---|
| 审批 | 申请人确认 draft 后才 pending；职责分离、冻结参数、截止时刻过期；投票/撤回/策略复核共用 CAS 和事务审计 | `workflow/store.py`、`workflow/lifecycle.py` |
| 动作执行 | 资源串行化，先检查前序阻塞；领取、续租和提交都验证 owner/version/租约，旧 worker 不得写回 | `workflow/worker.py` |
| 外部未知结果 | 成功、明确失败、未知三分类；未知先回查，不靠重新写恢复；漂移转人工核对 | `workflow/adapters/` |
| 回收 | active/revoking/compensating/manual_review 区分；提前归还推进已有 revoke，不重复建回收动作；failed 不代表副作用全部撤销 | `workflow/store.py` |
| 事项 | 状态与版本 CAS；关闭及关联清理同事务；重开不复活取消的任务/提醒；历史续接重新授权且同键不重复排队 | `workflow/task_lifecycle.py`、`workflow/task_history.py` |
| 检查点 | 输入冻结，恢复重新提供身份/回调；结果必须恢复执行后的 conversation_state；仅复用同输入结果 | `agents/checkpointing.py`、`workflow/task_checkpoints.py` |
| 聊天与 SSE | 租户/用户/会话隔离；owner 与内容指纹原子认领；冲突 409、同键重放；终态禁止追加，前端未知失败保留请求键 | `core/events.py`、`api/routes/chat.py`、`frontend/src/api/chat.ts` |
| UI 异步 | 后端/身份/请求代次快照过滤迟到结果；草稿删除后不再接受增量；PKCE state 校验且 verifier 只消费一次 | `frontend/src/lib/requestState.ts`、`auth.ts` |
| 记忆 | 压缩快照冲突可重试；摘要提交与待归档索引原子写入；向量成功后才删 outbox；画像成功才推进维护水位 | `memory/conversation_memory.py` |
| 文档 | 新分片写入与旧分片删除同 SQL 事务；embedding 更新比较原正文和模型，防止覆盖并发编辑 | `core/vector_store.py`、`tooling/knowledge_base.py` |
| 熔断/启停 | 半开探测取消释放同 generation，迟到回调不改变新探测；启动失败和正常停止均释放已获取资源 | `tooling/tool_manager.py`、`api/main.py` |
| 飞书 | inbox/outbox 先落库、幂等、按会话保序、租约重试；不将 API 接收等同于消息已送达 | `intake/im.py` |

取消不能撤回已经到达外部系统的请求，也不能强制停止已开始的线程。SQL 与 Redis 不组成分布式事务；恢复依靠确定性 ID、回执与回查，不宣称跨系统 exactly-once。Redis 崩溃恢复依赖部署持久化配置。

`TaskStore.configure_extensions` 在启动时接入权限、用量和生命周期；调度失败契约在 `workflow/task_ports.py`。不要把业务规则散入 API 或用额外容器包裹现有组合根。

验证：`test_protocol_regressions.py`、`test_workflow.py`、`test_task_capabilities.py`、`test_task_execution.py`、`test_shared_tasks.py`、`chatStream.test.js`；执行方法见 [开发入口](../CONTRIBUTING.md)。
