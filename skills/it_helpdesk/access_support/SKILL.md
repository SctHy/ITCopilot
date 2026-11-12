---
name: access-support
description: 密码、MFA、解锁与权限申请
keywords: [密码, 解锁, MFA, 验证码, 权限, GitLab, VPN账号]
intent_groups: [access]
write_tools: [request_access, reset_password, unlock_account]
---
- 申请字段以工具参数为准；SOP 里工具没有的字段（项目空间、有效期、审批人）不追问，用户给了就写进 reason。
- 只读用 readonly；管理员等不支持的角色如实说明，不换成其他角色申请。
- 用户只是咨询或要求跳过审批时，不调用写工具。
- 演示环境：审批通过后只改演示数据，真实账号由身份平台核验本人后处理。
