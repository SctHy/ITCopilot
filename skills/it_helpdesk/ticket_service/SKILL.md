---
name: ticket-service
description: IT 工单、优先级和人工升级
keywords: [工单, ticket, SLA, 升级, 人工]
agents: [ticket, triage, endpoint, network_software, access]
---
P1 为大范围中断，P2 为关键员工无法工作，P3 为单用户一般问题，P4 为咨询。建单工具只需 title 和 description：用户明确要求建单时直接创建待审批工单，把已查到的信息和仍缺的信息（如系统版本）写进 description，不要因缺少诊断字段先追问而不建单。工单本身就是待审批记录，审批即确认环节；用户已要求建单（包括“查完/诊断后建单”）时，同一轮内完成查询后接着建单，不要在聊天里再次征求确认。创建工单后必须返回可核验的 ticket_id；没有执行证据时不得声称变更完成。
