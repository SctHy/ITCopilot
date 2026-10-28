# 0006 默认使用通用 Agent，多 Agent 改为 LLM 规划

日期：2026-10-29 · 状态：已采纳

## 背景

分层对照（docs/evaluation-v3.md 第 6 节）中，规则规划的多 Agent 44/64，低于持有全部工具的单个通用 Agent 63/64。失败原因是：

- 关键词规则漏掉领域；
- 识别出多个领域时无条件追加建单；
- 固定 token 预算对三领域请求不够。

## 决定

- 规划改为 `LLMPlanner`：结构化输出 `make_plan`，经 `finalize()` 校验（Agent 类型合法、每类一步、最多 4 步）。失败时回退规则规划器。
- 只有用户明确要求时才保留建单步骤（`wants_ticket`，识别否定说法），建单步骤依赖其他全部步骤。
- token 预算按步骤数计算，每步 10000（`IT_COPILOT_PLAN_TOKENS_PER_STEP`）。
- 新增 `bounded_all`（专职 Agent 持有全部工具），作为对照。
- 默认 `IT_COPILOT_PLANNING_MODE=generalist`。

## 后果

- 修复后，同一批 42 个场景上多 Agent 由 26 升到 41，与通用 Agent（40）、`bounded_all`（42）无显著差异。多轮状态评测中，三者 pass^1 为 0.80–0.85，都高于单专职 Agent 的 0.62。
- 通用 Agent 的 token 更少、链路更短，所以作为默认。需要按领域隔离写工具、拆分提示词与监控时，使用 `bounded`。
- 工具规模扩大到几十个时，通用 Agent 的工具选择可能退化，需要重新评测。
