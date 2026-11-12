# ADR 0033：Agent 规格版本化、灰度与回滚

状态：已采纳

## 背景

单个服务 Agent 已经覆盖当前业务。需要管理的是改变行为的提示词、工具范围、技能和模型路由版本，以及发布的证据。

## 决定

`config/agents/*.yaml` 定义不可变的 `(name, version)` 规格。加载时验证工具、路由、技能路径与能力歧义，规范化内容计算 sha256。默认 1.0.0 保持原提示词、工具和技能正文；不拆成多个 Agent。编排器按规格 fingerprint 缓存运行实例，能力路由优先显式意图组，再优先显式租户。

`agent_rollouts` 保存 stable/canary 状态，用 Machine 校验迁移。`sha256(spec_name + scope_key) % 100` 决定分桶；租户名单额外限制灰度。第一轮的版本保存在会话状态，事项创建时保存到初始事件；迁移一次性回填旧事项。回滚只影响新会话，已有会话和事项有意保持原版本，避免中途改变处理规则。每个 API 进程最多缓存 5 秒。

发布前执行 `scripts/release_check.py --spec it_copilot --version <version>`：目标版本安全回归必须零越权，确定性线上回归通过，真实模型 stable/candidate 各三遍，候选对 stable 不劣（成功率差不低于 -2 个百分点，或 Wilson 区间覆盖），线上回归三遍通过。缺报告、失败报告和 fingerprint 不匹配均拒绝发起灰度。离线 CI 不将未运行的真实评测当作通过。

每分钟对最近 30 分钟的回合做版本比较；canary 至少 100 条且 stable 有数据时，Newcombe/Wilson 失败率差区间下界大于零、虚假声明/负反馈超过两倍或 P95 超过 1.5 倍，触发自动回滚和 critical 告警。操作与事件在同一事务审计。样本不足时不会判断退化。

## 后果

默认还是一个 Agent。离线假模型只能验证选择、粘性和状态迁移，不证明真实模型质量。真实版本发布报告尚未运行，默认版本的存在不等于某个新版本已获准灰度。单租户下接口需要 it_admin；多租户下全局发布配置沿用 ADR 0025 的运营方 platform_admin 权限边界，租户管理员不能改变其他租户的版本。

外部 MCP 断开时规格保留，只移除暂不可用的工具。技能及路由仍保留已有热加载机制，变更各自通过 skill_fp、route 和 change_events 追踪；上线后需要持续查看这些指纹。

## 验证

`.venv/bin/pytest -q tests/test_agent_registry.py tests/test_task_execution.py tests/test_turn_records.py tests/test_monitor.py`。

覆盖分桶稳定性和比例、能力优先级与歧义拒绝、默认指令与工具不漂移、租户名单、粘性、回滚后新旧会话差异、发布报告拒绝、管理权限、MCP 断开和合成失败版本自动回滚。PostgreSQL 使用 `IT_COPILOT_TEST_DATABASE_URL=...` 运行同一套存储测试。

创建共享事项时先通过已装配的意图识别端口获得实际 intent_group，再在写入初始事件的同一事务中冻结所选规格版本；首次 worker 回合复用识别缓存。发布离线回归通过 pytest 的 --agent-spec/--agent-version 参数运行并断言 trace 版本，避免候选门禁误跑默认规格。
