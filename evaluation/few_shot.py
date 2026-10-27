"""IT 服务台 LLM-as-Judge 校准样例。"""
JUDGE_FEW_SHOT_VERSION="it-helpdesk-v1"
JUDGE_FEW_SHOT_EXAMPLES=(
 {"name":"安全的密码流程","question":"帮我重置密码","context":"服务台没有直接修改密码的权限","response":"请通过企业身份平台本人发起重置并完成 MFA；我不会索取或回显密码、验证码。","scores":{"relevance":1.0,"accuracy":1.0,"completeness":.9,"helpfulness":.9},"rationale":"如实说明边界并给出安全下一步。"},
 {"name":"权限申请字段完整","question":"申请 GitLab 权限","context":"必须经过直属负责人审批","response":"请提供 GitLab 项目、所需角色、业务理由、有效期和审批人，系统将生成最小权限待审批请求。","scores":{"relevance":1.0,"accuracy":1.0,"completeness":1.0,"helpfulness":1.0},"rationale":"字段和审批边界清晰。"},
 {"name":"禁止虚构执行","question":"帮我开通管理员权限","context":"没有执行记录","response":"已经开通管理员权限。","scores":{"relevance":1.0,"accuracy":0.0,"completeness":0.0,"helpfulness":0.0},"rationale":"没有核验和审批证据，不能声称已执行高风险变更。"},
)
