# 0002 调用者身份只取自已验证令牌

日期：2026-10-28 · 状态：已采纳

## 背景

v2 中 `user_id`、`department` 来自聊天请求体，审批人 `actor` 也由客户端提交，所有管理操作共享一个管理员令牌。任何客户端都可以冒充他人、扩大检索范围或批准自己的申请。

## 决定

- 每个请求经 `current_principal` 依赖校验 Bearer JWT，生成 `Principal(subject, username, department, roles, employee_id, tenant)`。
- 两种令牌来源共用一条校验路径：`oidc`（JWKS 校验 RS256，核对 issuer 与 audience/azp）；`dev`（进程内 HS256 签发，仅用于开发、测试与评测，生产环境拒绝）。
- 请求体不再含身份字段；即使客户端发送也被忽略。
- 客户端幂等键 `client_request_id` 与调用者绑定（UUIDv5(tenant, subject, id)），不同用户使用相同值不会共享回执、trace 或审批。
- 对象级授权：trace、审批、审计、员工状态、事件流只对所有者，或审批人 / 管理员 / 审计员（按资源）开放。

## 后果

- 测试与评测需要令牌：`tests/conftest.py::auth()`、`evaluation.application.auth()`。
- Keycloak realm 由 `config/dev_users.yaml` 生成，两种模式看到同一批人。
- 令牌缺少 `department` 视为不可用，而不是默认一个宽泛范围。
