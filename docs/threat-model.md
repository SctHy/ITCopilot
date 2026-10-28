# 威胁模型

范围：对话 API、审批与执行工作流、事件流、知识检索。方法：按 STRIDE 列出威胁，每条对应控制措施与验证位置。“验证”列指向会在 CI 中失败的测试；没有测试的条目标为“未覆盖”。

## 资产与信任边界

- 资产：目标系统中的权限与账号状态、审批记录与审计日志、对话与 trace（可能含员工信息）、知识库文档（含部门受限文档）。
- 边界：浏览器 ↔ API（Bearer JWT）；API ↔ 模型网关（模型输出不可信）；API/worker ↔ 目标系统（服务账号）；知识库内容（不可信输入，会进入模型上下文）。

## 威胁与控制

| STRIDE | 威胁 | 控制 | 验证 |
|---|---|---|---|
| 仿冒 | 伪造、篡改、过期、`alg=none` 令牌 | JWKS/HS256 签名校验，固定算法列表，校验 iss/aud/exp；生产拒绝 dev 令牌 | `security_v3`：`authn_*` 5 项；`test_dev_tokens_are_refused_in_production`；`test_keycloak_tokens_map_to_principals`（真实 Keycloak） |
| 仿冒 | 在请求体中声明他人 user_id/department | 身份只取自令牌，请求体字段被忽略 | `spoof_body_identity`、`test_identity_comes_from_token_not_request_body` |
| 篡改 | 修改已提交审批的参数后再批准 | 冻结快照 + SHA-256 哈希；同 ID 不同参数拒绝 | `tamper_payload_hash`、`test_frozen_hash_and_concurrent_approvals_execute_once` |
| 篡改 | 提示注入让模型申请管理员权限或给他人改密码 | 写工具只生成待审批；策略限制角色与时长；仅限本人申请 | `injection_kb_skip_approval`、`policy_*` 4 项 |
| 抵赖 | 审批人否认批准 | 决定按 subject 记录（唯一约束），审计日志记录每次状态迁移与执行者 | `test_http_chat_approval_execution_and_replay` 审计序列 |
| 信息泄露 | 读取他人 trace、审批、审计、员工状态、事件流 | 对象级授权；`/trace/tools` 仅管理员/审计员 | `idor_cross_user_reads`（7 个端点 + 列表） |
| 信息泄露 | 通过 `department` 参数检索其他部门文档 | 部门取自令牌（管理员除外）；向量召回自适应扩窗后过滤 | `spoof_search_department`、`test_department_filter_is_not_crowded_out_by_other_departments` |
| 信息泄露 | 不同用户使用相同 `client_request_id` 读到对方回执 | 请求 ID 按 (tenant, subject, client id) 派生 | `idor_client_request_id_collision` |
| 拒绝服务 | 同一请求并发提交导致重复执行 | 会话锁 + 回执；事件流按请求认领，重复提交只订阅 | `test_http_chat_approval_execution_and_replay`、`test_live_stream_reports_tools_before_completion_and_resumes` |
| 拒绝服务 | 外部系统持续超时拖垮 Agent | 统一工具执行层：超时、熔断、只读重试 | `test_timeout_and_circuit_breaker` |
| 权限提升 | 审批人批准自己的申请或针对自己账号的申请 | 职责分离（按 subject 与 employee_id），API 与审批库双重检查 | `sod_*` 5 项、`test_separation_of_duties_and_authority` |
| 权限提升 | 单人批准需要双人审批的高权限 | 策略声明审批人数；同一审批人重复投票只算一票；版本号 CAS 防并发漏计 | `sod_two_person_rule_single_approver`、`test_developer_access_needs_two_distinct_approvers_even_concurrently`（含 PostgreSQL） |
| 权限提升 | 权限到期未回收，或回收误删原有/他人修改的权限 | 授权成功同事务登记回收；baseline 先持久化；回收前漂移检查 | `test_grant_expires_and_is_revoked`、`test_revocation_never_removes_preexisting_or_drifted_access`、`test_real_gitea_grant_expiry_and_drift` |
| 篡改 | 结果未知时重试造成重复变更 | 三分类错误；先回查再重做；租约 + CAS | `test_ambiguous_outcome_is_reconciled_before_retry`、`test_worker_crash_after_effect_is_recovered_without_repeating` |

## 已知未覆盖

- 令牌吊销与会话失效：依赖短有效期（Keycloak 15 分钟），API 不查询吊销列表。
- 服务账号权限：compose 中 Gitea 服务账号是管理员；生产应使用仅能管理指定仓库协作者的最小权限账号。
- 速率限制与配额：未实现，应在网关层处理。
- Keycloak 开启了密码模式（`directAccessGrantsEnabled`）便于脚本获取沙箱令牌，任何共享部署都应关闭。
- trace 与事件中的个人信息只做了模式脱敏（`core/redaction.py`），没有按字段的数据分级。
- 模型网关与外部 MCP 服务的数据外发：外部 MCP 默认关闭；开启后员工问题会发送到第三方。
