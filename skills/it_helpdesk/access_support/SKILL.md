---
name: access-support
description: 身份、密码、MFA 和权限申请 SOP
keywords: [密码, 解锁, MFA, 验证码, 权限, GitLab, VPN账号]
agents: [access, triage]
---
当前工具是本地演示适配器，不接入真实身份平台。权限申请的工具必填字段为 employee_id、system、role、reason；只读角色使用 readonly，系统仅支持 GitLab/Jira/VPN。字段齐全且用户明确要求申请时创建待审批记录，不把未实现的有效期或审批人字段当成工具必填项。若用户仅咨询或要求跳过审批，不调用写工具；禁止将管理员权限请求擅自替换成只读申请。
密码重置与解锁工具只创建本地待审批记录，需要 employee_id；批准后仅修改演示数据库。真实企业账号必须由身份平台核验本人并执行。聊天中不得接收密码、验证码、恢复码或私钥；没有执行结果不得声称已开通或已重置。
