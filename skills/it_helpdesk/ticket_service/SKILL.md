---
name: ticket-service
description: 工单优先级与建单时机
keywords: [工单, ticket, SLA, 升级, 人工]
agents: [ticket, triage, endpoint, network_software, access]
---
- 优先级：P1 大范围中断，P2 关键员工无法工作，P3 单用户一般问题，P4 咨询。
- 用户要求建单（包括“诊断后建单”）时，同一轮查完就建，缺的信息写进 description，不再征求确认。
- 建单只生成待审批记录，审批通过后才有工单号；不要向用户承诺立即给出单号。
