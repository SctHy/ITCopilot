# Changelog

## Phase 9 — 代码体检与修复

- 意图识别：去掉针对评测集三条追问用例写死的特判，改为通用追问继承规则（同组保留具体意图、非领域意图继承上一轮组、跨领域视为换话题）。
- 本地 n-gram embedding 由进程随机加盐的 `hash()` 改为 `zlib.crc32`，意图投票与校准指标跨进程可复现。
- LLM 不可用时紧急度改由规则判定（此前恒为 normal）。
- 修复 `/trace/tools` 被 `/trace/{request_id}` 遮蔽而不可达；后台任务保存引用并记录异常；并行 Agent 全部失败时返回明确的 failed 状态。
- 重写 Agent 编排、意图识别、工具定义与 API 入口的分号压行代码；路由由 `INTENT_GROUPS` 推导，消除重复映射；API 不再访问其他模块私有成员。
- 用 17 个真实断言替换 7 个不调用项目代码的空测试；后端离线测试共 92 项。

## Phase 8 — 迁移到 PydanticAI 与 Python 3.14

- Agent 运行时从手写 AsyncAnthropic tool_use 循环迁移到 PydanticAI `Agent`；白名单、Schema 校验、写操作守卫、trace、3 轮上限与错误分类保持不变。
- 意图识别、查询改写、重排、画像提炼改为 `ToolOutput` 结构化输出；摘要与 LLM Judge 使用 `pydantic_ai.direct`。
- 新增 `core/llm.py` 模型工厂与 `IT_COPILOT_LLM_PROVIDER`（anthropic 经网关 / openai 直连）；网关修复 `tool_choice` 一律被改写为 auto 的问题。
- 运行环境升级到 uv 管理的 Python 3.14，全部依赖升级到最新版本；后端离线测试增至 82 项。

## Phase 7 — 工程化与可观测

- 增加 `/chat/stream` SSE 状态流、前端工具/审批状态展示。
- 统一 `/trace/{request_id}` 回放计划、步骤、工具、记忆、token、耗时、fallback 与审批事件。
- 扩充离线后端边界测试至 71 项，保留前端 18 项 node:test。
- 重写项目主页 README，补充架构、指标、生产替换边界、设计取舍和 MIT License。

## Phase 1–6 — IT-Copilot 基础能力

- 基于多智能体编排骨架搭建 IT-Copilot 企业内部 IT 服务台，聚焦账号、权限、网络、设备与工单五类问题。
- 恢复 AsyncAnthropic/tool_use、8082 网关、多 Agent 路由、并行执行与工具可靠性层。
- 完成 Redis 锁与幂等、三级记忆、BM25/vector/hybrid RAG、Planner、HITL 审批和安全评测。
- 完成意图置信度温度校准：47 条固定集 ECE 0.394681→0.258583，Brier 0.168457→0.114710。
