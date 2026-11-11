# 威胁模型

范围：对话 API、审批与执行工作流、事件流、知识检索。方法：按 STRIDE 列出威胁，每条对应控制措施与验证位置。“验证”列指向会在 CI 中失败的测试；没有测试的条目标为“未覆盖”。

## 资产与信任边界

- 资产：目标系统中的权限与账号状态、审批记录与审计日志、对话与 trace（可能含员工信息）、知识库文档（含部门受限文档）。
- 边界：浏览器 ↔ API（Bearer JWT）；API ↔ 模型网关（模型输出不可信）；API/worker ↔ 目标系统（服务账号）；知识库内容（不可信输入，会进入模型上下文）。

## 威胁与控制

| STRIDE | 威胁 | 控制 | 验证 |
|---|---|---|---|
| 仿冒 | 伪造、篡改、过期、`alg=none` 令牌 | JWKS/HS256 签名校验，固定算法列表，校验 iss/aud/exp；生产拒绝 dev 令牌 | `security_v3`：`authn_*` 5 项；`test_dev_tokens_are_refused_in_production`；`test_keycloak_tokens_map_to_principals`（真实 Keycloak） |
| 仿冒 | 离职员工或泄露的令牌在过期前继续使用 | 按身份的吊销水位线：签发不晚于水位线的令牌一律 401；管理员吊销、本人退出所有设备；查询失败拒绝请求 | `test_admin_revokes_a_person_and_old_tokens_stop_everywhere`、`test_watermark_refuses_older_tokens_and_never_moves_back`、`test_lookup_failure_refuses_the_request` |
| 仿冒 | 在请求体中声明他人 user_id/department | 身份只取自令牌，请求体字段被忽略 | `spoof_body_identity`、`test_identity_comes_from_token_not_request_body` |
| 篡改 | 修改已提交审批的参数后再批准 | 冻结快照 + SHA-256 哈希；同 ID 不同参数拒绝 | `tamper_payload_hash`、`test_frozen_hash_and_concurrent_approvals_execute_once` |
| 篡改 | 提示注入让模型申请管理员权限或给他人改密码 | 写工具只生成待审批；策略限制角色与时长；仅限本人申请 | `injection_kb_skip_approval`、`policy_*` 4 项 |
| 抵赖 | 审批人否认批准 | 决定按 subject 记录（唯一约束），审计日志记录每次状态迁移与执行者 | `test_http_chat_approval_execution_and_replay` 审计序列 |
| 信息泄露 | 读取他人 trace、审批、审计、员工状态、事件流 | 对象级授权；`/trace/tools` 仅管理员/审计员 | `idor_cross_user_reads`（7 个端点 + 列表） |
| 信息泄露 | 通过 `department` 参数检索其他部门文档 | 部门取自令牌（管理员除外）；向量召回前在 SQL 中过滤 | `spoof_search_department`、`test_department_filter_is_not_crowded_out_by_other_departments` |
| 信息泄露 | 匿名 MCP 检索部门受限知识 | MCP 检索固定公共部门 `*`，不接受客户端提供部门 | `test_anonymous_mcp_can_only_retrieve_public_documents` |
| 信息泄露 | 不同用户使用相同 `client_request_id` 读到对方回执 | 请求 ID 按 (tenant, subject, client id) 派生 | `idor_client_request_id_collision` |
| 信息泄露 | 不同租户同名主体共享私聊记忆、旧审批或 trace | 记忆/回执/SSE 使用结构化租户用户作用域；审批、替代操作与 trace 校验租户；旧无归属记忆不兜底 | `tests/test_isolation_and_history.py` |
| 信息泄露 | 同部门审批人读取不相关事项，或通过历史关联绕过撤权 | 显式成员/指定审批人访问；历史 SQL 候选及摘要再次授权；关联不复制内容或授予权限 | `test_new_room_denies_same_department_reviewer_until_explicit_invite`、`test_reference_does_not_copy_private_source_into_shared_room` |
| 权限提升 | 继续历史流程时重复执行旧批准操作 | 原事项幂等续接事件，读取当前状态；续接回合禁用写工具 | `test_continuation_is_idempotent_readonly_and_uses_original_task` |
| 拒绝服务 | 同一请求并发提交导致重复执行 | 会话锁 + 回执；事件流按请求认领，重复提交只订阅 | `test_http_chat_approval_execution_and_replay`、`test_live_stream_reports_tools_before_completion_and_resumes` |
| 拒绝服务 | 单个身份连续发请求占满模型额度与 worker | 按租户＋用户的 GCRA 限流，网页、事项、飞书入口共用预算；Redis 原子计数，多进程共享；429 + Retry-After | `test_chat_returns_429_with_retry_after_per_identity`、`test_redis_backend_shares_one_budget_across_processes`、`test_im_messages_get_one_short_reply_instead_of_model_work` |
| 信息泄露 | 员工原话在飞书收发记录与情景记忆中无限期留存 | 保留期清理（默认 30 / 90 天）；未结审批关联的消息保留到审批结束；审计记录不清理 | `test_old_settled_messages_and_memories_are_deleted_in_batches`、`test_messages_behind_an_open_approval_are_kept_until_it_settles` |
| 权限提升 | 策略收回代他人申请的角色后，代申请的单据仍被批准或权限仍生效 | 审批保存申请人提交时的角色快照，复核时一并检查 `on_behalf_roles` | `test_on_behalf_requests_are_rechecked_against_the_role_they_were_filed_under` |
| 篡改 | 提示注入诱导 Agent 撤回用户的申请或归还用户的权限 | Agent 没有撤回/归还工具；对话只按身份召回本人项目作为数据和卡片，动作必须由本人点击并经服务端校验 | `test_chat_shows_cards_and_the_model_cannot_withdraw` |
| 抵赖 | 管理员否认删除知识库文档、触发策略复核或数据清理 | 成功的管理操作写入只追加的 `admin_audit`（操作人、对象、明细），审计员与管理员按租户查看 | `test_knowledge_changes_are_attributed_and_failed_changes_leave_nothing`、`test_only_admins_and_auditors_read_their_own_tenant` |
| 权限提升 | 借远程锁定申请锁住他人的设备 | 员工只能为本人账号申请；适配器执行前核对设备属于申请中的员工，不符在任何副作用之前失败 | `test_a_device_held_by_someone_else_is_never_locked`、`test_tool_only_proposes_and_only_for_the_callers_own_account` |
| 拒绝服务 | 外部系统持续超时拖垮 Agent | 统一工具执行层：超时、熔断、只读重试 | `test_timeout_and_circuit_breaker` |
| 权限提升 | 审批人批准自己的申请或针对自己账号的申请 | 职责分离（按 subject 与 employee_id），API 与审批库双重检查 | `sod_*` 5 项、`test_separation_of_duties_and_authority` |
| 权限提升 | 单人批准需要双人审批的高权限 | 策略声明审批人数；同一审批人重复投票只算一票；版本号 CAS 防并发漏计 | `sod_two_person_rule_single_approver`、`test_developer_access_needs_two_distinct_approvers_even_concurrently`（含 PostgreSQL） |
| 权限提升 | 权限到期未回收，或回收误删原有/他人修改的权限 | 授权成功同事务登记回收；baseline 先持久化；回收前漂移检查 | `test_grant_expires_and_is_revoked`、`test_revocation_never_removes_preexisting_or_drifted_access`、`test_real_gitea_grant_expiry_and_drift` |
| 权限提升 | 策略收紧后，按旧规则提交的申请仍被批准或执行，已开通的权限照旧有效 | 按版本号复核：待审批失效或提高审批人数，未执行的动作取消，授权提前回收；只收紧不放宽（ADR 0008） | `tests/test_policy_revalidation.py` 9 项 |
| 仿冒 | 伪造飞书回调，或在消息正文里冒充他人 | 校验 verification token，配置 Encrypt Key 时再校验签名并解密；身份只取自事件头的 user_id/open_id 和映射表，正文不参与；未映射的人不受理 | `test_plaintext_events_need_the_verification_token`、`test_encrypted_events_are_signed_and_decrypted`、`test_identity_comes_from_the_mapping_file_only` |
| 拒绝服务 | 飞书重推或进程崩溃导致重复回复、重复建单 | 消息 ID 作主键落库；回复与通知按幂等键入出站 outbox；飞书 uuid 去重 | `test_message_is_answered_once_even_when_the_platform_retries` |
| 仿冒 | 截获一次合法签名的飞书回调，事后重放 | 签名覆盖的 `X-Lark-Request-Timestamp` 须在 ±`FEISHU_MAX_CLOCK_SKEW_S`（默认 300 秒）内，格式非纯数字直接拒绝；窗口内的重放由消息 ID 去重 | `test_signed_requests_cannot_be_replayed_outside_the_time_window` |
| 权限提升 | 他人撤回别人的申请，或撤回与批准并发导致既执行又撤回 | 撤回只限同租户申请人本人；与投票共用版本号 CAS，只有一方生效，撤回后投票不再生效 | `test_requester_withdraws_a_pending_request_and_nothing_executes`、`test_withdraw_racing_an_approval_settles_exactly_once` |
| 权限提升 | 借“提前归还”删除他人权限，或删掉员工原有/被他人改动的权限 | 仅申请人、权限持有人、同租户管理员可归还；只提前已排期的回收动作，执行时仍做漂移检查 | `test_holder_or_admin_may_release_but_other_tenants_may_not`、`test_release_keeps_preexisting_and_drifted_access` |
| 篡改 | 结果未知时重试造成重复变更 | 三分类错误；先回查再重做；租约 + CAS | `test_ambiguous_outcome_is_reconciled_before_retry`、`test_worker_crash_after_effect_is_recovered_without_repeating` |

## 已知未覆盖

共享事项边界见 [ADR 0012](adr/0012-shared-service-tasks.md) 与 [ADR 0016](adr/0016-identity-scope-and-task-history.md)：按租户及显式成员/指派限制访问，保留私人会话命名空间；审批人讨论与结果回调不可生成写申请；事件回复和审批原子提交，过期租约禁止提交。验证见 `tests/test_shared_tasks.py` 与 `tests/test_isolation_and_history.py`。共享上下文不使用私人记忆。

- 令牌吊销只在本服务生效（ADR 0019）：不查询 IdP，离职仍需在 IdP 停用账号；飞书身份不受令牌水位线约束；吊销在其他进程最多延迟 5 秒生效。
- 服务账号权限：compose 中 Gitea 服务账号是管理员；生产应使用仅能管理指定仓库协作者的最小权限账号。
- 配额：每身份限流已实现（ADR 0018，`tests/test_rate_limit.py`），但没有租户级费用配额，也不防分布式攻击；入口层 IP 限流仍应在网关处理。Redis 故障时限流放行。
- Keycloak 开启了密码模式（`directAccessGrantsEnabled`）便于脚本获取沙箱令牌，任何共享部署都应关闭。
- trace 与事件中的个人信息只做了模式脱敏（`core/redaction.py`），没有按字段的数据分级。
- 模型网关与外部 MCP 服务的数据外发：外部 MCP 默认关闭；开启后员工问题会发送到第三方。
- 飞书身份映射是手工维护的静态文件；映射错误会让一个人以另一个人的身份申请，生产环境应对接企业目录。
- 飞书回调只在配置 Encrypt Key（启用签名）时校验时间窗；只用 Verification Token 的明文回调没有可信时间戳，重放只能靠消息 ID 去重。
- 管理操作审计在操作成功后写入，与操作本身不在同一事务：写入失败时操作已生效但缺少记录（会返回 500）。
