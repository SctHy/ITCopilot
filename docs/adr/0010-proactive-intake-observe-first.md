# 0010 告警主动建单：规则准入、先观察后自动、不绕过审批

日期：2026-11-03 · 状态：已废弃（同日移除：功能不在演示主线上，Prometheus 与 Alertmanager 只为它而部署；迁移 0006 删除 `proactive_decisions` 表）

## 背景

以前只有员工来问，Agent 才处理。监控已经发现了问题（工具熔断、模型服务故障），却要等有人报障才会建单。让 Agent 主动干活有两个风险：一是误判产生噪声，浪费审批人和 IT 的注意力；二是主动动作绕过人工把关。

这里参考了 DeepSeek dsh-superagent 处理“推断出的工作”的方式：先过确定性准入门，再用 `off / observe / auto` 三种受理模式控制；从 observe 开始，人确认它“本来会做的事”是对的，再切到 auto。

## 决定

- **来源**：Alertmanager webhook（v4）推到 `POST /proactive/alerts`，用 `IT_COPILOT_ALERT_TOKEN` 做 Bearer 共享密钥校验。没有配置密钥时，这个端点不存在（返回 404）。
- **准入**：用 `config/alert_rules.yaml` 里的规则按标签做确定性匹配，不调用模型。必须**恰好命中一条**已启用的规则才受理；命中多条视为有歧义，直接忽略（规则顺序不代表优先级）。同一告警（按 fingerprint）在冷却期内只受理一次。
- **模式**：`off` 只记录不受理；`observe`（默认）记录 `would_accept` 和完整的工单草稿，但不建单；`auto` 提交一个 `create_ticket` 审批。
- **不绕过审批**：auto 模式以服务身份 `system:proactive`（目录中的 S0001）提交的是“待审批的建单申请”，最后仍由审批人决定（ADR 0001）。同一告警重新触发时，同一会话（`alert:<fingerprint>`）里新的申请会替代还没审批的旧申请（ADR 0007）。
- **证据**：草稿包含告警标签、注解、来源链接、命中的规则和规则版本；规则配置了 `knowledge_query` 时，还会附上知识库检索到的前 3 条结果，但只保留向量相似度不低于 `knowledge_min_similarity`（默认 0.45）的条目。hybrid 检索总会返回 top-k，不相关的文章只会浪费审批人的注意力。0.45 只是在演示知识库上用 4 条查询观察到的经验值（相关条目 0.49，不相关条目都不超过 0.45），没有经过验证。
- **决定记录**：每个告警的每次触发（`fingerprint + startsAt`）只记录一条决定（`proactive_decisions`），所以 Alertmanager 的重复推送不会改变结果。`GET /proactive/decisions?decision=would_accept` 用来复核观察期的结果。指标是 `it_copilot_proactive_decisions_total{mode,decision}`。

## 后果

- 观察期的 `would_accept` 记录可以直接作为评测集：人工标注“该不该建单”，就能算出切换到 auto 前的准确率。
- 规则匹配是确定性的，可解释、可测试（`tests/test_proactive_alerts.py`），但不能理解告警的语义。需要语义判断时，可以在准入门之后再加一层模型研判，规则仍然是第一道门。
- 告警恢复只记录为 `resolved`，不会撤回仍在待审批的建单申请，由审批人自行判断。
- compose 的 `observability` profile 里加了 Alertmanager，已有的 Prometheus 告警（工具熔断、模型故障、MCP 断开）会直接进入主动受理。
