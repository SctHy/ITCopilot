# 0014 共享事项的协作管理与独立通知

日期：2026-11-11 · 状态：已采纳

默认部门访问规则已由 [ADR 0016](0016-identity-scope-and-task-history.md) 取代；以下保留当时的决策记录。

## 结果与模块边界

共享事项补齐六项功能：待办取消/改派、历史搜索/分页/个人归档、附件证据、站外通知/提醒升级、成员管理/指定审批人、Agent 进度建议。

| 能力 | 独立模块 | 约束 |
| --- | --- | --- |
| 取消与改派 | TaskCollaborationService | 创建人或管理员操作；原因留痕；旧回答人不能继续答复 |
| 搜索与归档 | TaskQuery | 授权范围内搜索标题及讨论；按人归档；不关闭事项 |
| 附件 | TaskFiles | 文件与证据事件同事务保存；下载时重新授权 |
| 成员和指派 | TaskAccess | 只引用经过登录验证的身份；邀请不授予组织角色 |
| 通知 | TaskNotifications / TaskNotificationWorker | 事件游标、投递队列、租约和重试；传输器通过构造参数注入 |
| 进度建议 | TaskProgressAdvisor / TaskSuggestions | 模型只生成草稿；人工采纳时校验证据和进度版本 |

TaskStore 依赖注入的访问策略与 TaskEffects 端口，不导入产品扩展。CompositeTaskEffects 在回合完成事务内组合审批产物和建议草稿。TaskCapabilities 负责业务组合和工作区查询，api/main.py 负责实例装配。路由通过 task_dependencies 共享依赖，不相互导入。Vue 面板通过 taskClient 和独立请求状态提交操作，不调用其他面板内部实现。

## 成员、审批与历史

- 申请人和同租户 IT 管理员保留访问权。未设置成员身份的同部门审批人沿用原权限。协作者可讨论，观察者只读；观察者可管理自己的归档、阅读位置和通知订阅。
- 申请人/管理员可添加已登录过的同租户成员。成员移除覆盖默认部门访问；先改派或取消其未完成待办，并解除审批指派，才能移除或降为观察者。
- 只有 IT 管理员可以指定审批人。被指派者仍须具有审批角色、事项写权限，并满足原有职责分离和人数规则。清空指派恢复原角色规则。
- 指派检查覆盖共享事项投票、旧审批 API、直接工作流调用。指派、移除与投票用同一事项行锁串行化。
- 身份目录保存最近一次认证请求的公开身份快照，不同步企业全员目录；会话身份仍由令牌校验。组织角色变化在后续认证请求时刷新目录。
- `/task-inbox` 支持 q、status、category、archived、offset、limit，返回 total 和 has_more。类别筛选在分页前完成，不再截断到最近 100 个事项。当前实现先读取匹配的事项元数据再计算聚合计数，适合现有规模；大租户应改为批量聚合 SQL。
- 网页首次读取最近 50 条讨论，向前分页；轮询合并连续历史，离线过久出现间隔时从最新一页重新展开。归档只改变当前用户的列表。

## 附件与建议

附件上限 5 MiB，支持 txt/log/csv/json/png/jpg/jpeg/pdf。保存内容、文件名、大小与 SHA-256；同一上传请求重试幂等，改变内容会冲突。附件生成可引用的证据消息。下载强制 attachment、octet-stream 和 nosniff，不在页面内解释上传内容。此版本以数据库二进制字段保存，未包含 OCR、内容检索、病毒扫描或存储保留策略。

进度建议使用 PydanticAI 结构化输出 Schema，最多两次模型请求、总计约 20 秒，无业务工具。非法证据引用会移除，无证据的 done 降为 working。失败保留主回复和审批产物；已返回的模型 token 计入累计预算。建议记录生成时的进度版本；人工采纳使用版本比较，不能覆盖后来的编辑，也不能自动验收、投票或执行业务变更。

## 通知配置与恢复

服务端配置可信收件地址；浏览器只能选择当前身份已有渠道，不能填写任意 URL 或收件人。以下均为占位符：

```dotenv
IT_COPILOT_PUBLIC_WEB_URL=https://it.example.invalid
IT_COPILOT_TASK_CONTACTS={"default:dev:alice":{"email":"alice@example.invalid"},"default:dev:bob":{"email":"bob@example.invalid","feishu":"ou_example"}}
IT_COPILOT_SMTP_HOST=smtp.example.invalid
IT_COPILOT_SMTP_PORT=587
IT_COPILOT_SMTP_FROM=it-copilot@example.invalid
IT_COPILOT_SMTP_USERNAME=it-copilot
IT_COPILOT_SMTP_PASSWORD=replace-via-secret-store
```

联系人键为 `tenant:subject`；OIDC 使用实际令牌主体，不能照抄 dev 示例。SMTP 使用 STARTTLS。飞书复用已有 FEISHU_APP_ID/FEISHU_APP_SECRET 配置及客户端，联系人值为 open_id。渠道未配置时网页显示不可用。环境变量可写入 `.env`；现有 Compose API 的 env_file 会加载。Compose 本地网页链接需设为 `http://127.0.0.1:8080`。

用户按事项订阅一个渠道。申请人/管理员可选择有访问权且配置同渠道地址的升级对象；网页从事项成员中选择目标。待办逾期、手动提醒及审批临期事件均可升级；审批临期默认在到期前一小时发生。通知只包含事项标题和登录链接，不复制讨论、附件或审批载荷。

事件扫描使用持久化游标；新订阅不补发历史。事件、收件人与渠道组成幂等投递 ID。worker 独立于 Agent worker，认领租约 60 秒，最多尝试 5 次；失败退避并记录状态，重启后继续。发送前复查订阅、最新已知身份和事项访问权；已关闭的待办不再发送逾期提醒。SMTP 用固定 Message-ID、飞书用固定 uuid，但外部系统不保证 exactly-once，超时后可能重复；已提交给外部服务的消息不能撤回。已入队的审批临期事件可能在投票后送达，链接内状态以服务端为准。

## 验证

```bash
.venv/bin/python -m pytest -o addopts='' -q tests/test_task_product.py
# PostgreSQL：在隔离测试库配置 IT_COPILOT_TEST_DATABASE_URL 后执行同一命令。
.venv/bin/python scripts/evaluate_shared_tasks.py --live --cases approve,clarification,acceptance --repeats 1 --output /tmp/task-product-live.json
```

产品测试覆盖两条审批入口、跨租户拒绝、观察者/移除权限、附件证据、103 个事项分页、建议版本冲突/并发采纳、通知重试/升级/退订/权限撤销/租约到期。测试不向真实邮箱或飞书投递。真实模型评测额外检查建议入库。
