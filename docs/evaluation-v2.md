# IT-Copilot v2：实验协议与结果边界

本轮目标是验证业务状态、补齐故障恢复与对照实验。所有业务记录是本地演示数据；没有企业用户研究或生产接入。数据集由项目作者编写，未进行独立人工双标。

## 运行环境与复现

- 模型：本地 Anthropic→OpenAI 网关连接 DeepSeek Flash；结构化输出要求强制工具选择。
- 当前 DeepSeek thinking 模式不支持 required / named tool_choice，因此本地未提交的 `gateway/.env.gateway` 显式配置 `OPENAI_THINKING_TYPE=disabled`，不修改或降低工具选择约束。[官方说明](https://api-docs.deepseek.com/api/create-chat-completion/)
- 应用评测使用同一 `api.main.lifespan` 与 ASGI HTTP 路由，真实 Redis、临时 Chroma 与 SQLite。不是浏览器/网络传输压力测试。
- 专用 Redis 监听 127.0.0.1:16379，不启动系统服务，不清空任何现有 Redis。每次使用唯一用户/会话 ID；Chroma/SQLite 目录由临时目录隔离。
- 默认应用意图策略为 `llm`，有明确来源标签的规则兜底；保留 `pattern/embedding/fusion` 配置。停用旧的样本内温度，候选需显式安装。
- 后台画像提炼在在线评测中关闭，避免无关模型调用；情景记忆、会话状态、审批与工具检索仍走实际代码。

```bash
redis-server --port 16379 --bind 127.0.0.1 --save '' --appendonly no
./gateway/run_gateway.sh

# 新终端，已安装 requirements-dev.txt
INTENT_EMBEDDING_PROVIDER=local .venv/bin/python -m pytest -q
.venv/bin/python scripts/evaluate_intent_ablation.py --live
.venv/bin/python scripts/calibrate_intents.py             # 只生成候选，--install 才修改运行时配置
.venv/bin/python scripts/evaluate_rag_ablation.py --live
.venv/bin/python scripts/evaluate_memory_ablation.py
.venv/bin/python scripts/evaluate_recovery.py
.venv/bin/python scripts/evaluate_application.py
.venv/bin/python scripts/evaluate_application.py --dataset evaluation/datasets/application_holdout_v2.json --repeats 3 --output application_holdout_live.json
.venv/bin/python scripts/judge_application_report.py --limit 6
```

模型实验消耗 API token。`evaluate_all.py` 仍运行 v1 离线 smoke，不能代替 v2 在线实验。`evaluate_dialogs.py` 已改为完整应用评测入口；旧的编排器级 evaluator 保留为单元测试对象。

## 意图识别：分开校准、测试与来源

数据：[intent_v2.json](../evaluation/datasets/intent_v2.json)，22 类 × 4 条，共 88 条；每类前两条用于校准、后两条用于测试。原始 LLM 与本地信号每条只采集一次，四种策略复用同一组信号，避免模型重复采样掩盖投票差异。校准只读取 `split=calibration`，测试标签不参与拟合。

报告：[intent_ablation_live.json](../evaluation/reports/intent_ablation_live.json)。88 次分类模型调用均成功。

| 策略 | 测试正确数 / 44 | Macro-F1 | ECE 前 → 后 | Brier 前 → 后 |
|---|---:|---:|---:|---:|
| 规则 | 13 | 0.2652 | 0.0693 → 0.1286 | 0.1185 → 0.1365 |
| 本地 n-gram 向量 | 8 | 0.1242 | 0.0847 → 0.0994 | 0.1535 → 0.1552 |
| LLM | 44 | 1.0000 | 0.1075 → 0.0146 | 0.0123 → 0.0044 |
| 融合 | 44 | 1.0000 | 0.4529 → 0.3649 | 0.2099 → 0.1654 |

结论：本次未证明融合优于 LLM；本地弱信号降低融合置信度，可能导致不必要澄清。默认应用路由采用较简单的 LLM 主路由。这一决策参考了本次实验，因此 44/44 不能当作调参后新系统的完全独立泛化验证。组级校准样本仍小，默认不部署候选参数。配置按策略、模型、embedding 来源绑定；失败回退不能套用在线模型的温度。

历史 `intent_latest.json` 的 47/47 来自 `llm_calls=0`，只属于本地 smoke。温度校准只改变置信度，不改变类别。

## RAG：扩大干扰与分开比较阶段

数据：[60 篇文档](../evaluation/datasets/rag_v2_corpus.json)、[40 条查询](../evaluation/datasets/rag_v2_queries.json)。包含 20 个主题，各有当前研发 SOP、财务专用版本与历史背景干扰。每主题有一条语义查询、一条关键词查询。部门过滤为数据范围过滤，不是身份授权。

报告：[rag_ablation_v2.json](../evaluation/reports/rag_ablation_v2.json)。40 查询的 Recall@3 / MRR：

| 检索策略 | Recall@3 | MRR | 部门越界返回数 |
|---|---:|---:|---:|
| BM25 | 0.9500 | 0.8833 | 0 |
| vector（Chroma 默认模型） | 0.5250 | 0.4458 | 0 |
| hybrid（RRF） | 0.7250 | 0.6542 | 0 |

固定前 12 条配对子集：原 hybrid 为 9/12（0.75），加入查询改写与重排后为 11/12（0.9167），MRR 0.6944 → 0.9167；平均检索耗时约 51ms → 2318ms。这里比较的是配对子集，不把 12 条与完整 40 条混用。

当前中文词法检索在这组数据上更强；不能宣称 Hybrid 必然优于 BM25。`bm25` 模式已避免无用的向量查询。没有评测真实员工问题、答案忠实度或无答案识别率；有 citation 字段不等于忠实于证据。保留各查询原始排名以供错误分析。

## 应用：开发回归与冻结留出

开发集：[24 场景](../evaluation/datasets/application_v2.json)，覆盖申请、审批拒绝、多轮补槽/纠正、建单、诊断、咨询与越权请求。每策略执行一次，前后都是实际模型调用：

- [修改前](../evaluation/reports/application_development_before.json)：single 13/24，bounded 13/24。
- [组合修复后](../evaluation/reports/application_live.json)：single 23/24，bounded 22/24。
- 修复包括：新建工单错误要求已有 ticket_id、权限角色 Schema 与执行器不一致、技能文档要求未实现的工具字段、多轮返回仍报告旧缺槽位、弱本地信号导致误澄清、复合诊断未触发规划。
- 多处同时修改，不能把改善全部归因于某一项；这是开发集结果，不是独立测试成功率。

另一组 [12 个冻结留出场景](../evaluation/datasets/application_holdout_v2.json)，每策略各跑 3 次，交替执行策略顺序。[原始记录](../evaluation/reports/application_holdout_live.json)：

| 策略 | 成功 / 运行数 | 结果 |
|---|---:|---|
| single | 36/36 | 该小样本全部通过业务断言 |
| bounded | 33/36 | 同一复合诊断任务的三次运行都触发 12000 token 波次间预算保护，未进入建单步骤 |

不同重复共享题目及模型服务，并非 36 个独立任务。LLM 意图缓存和检索缓存也是运行时的一部分；不将时延推广成生产性能，也不宣称多 Agent 提升任务成功率。single 基线使用意图选出的单个角色工具，不能与“一个拥有全部工具的通用 Agent”混淆。记录中的 token 只覆盖 Agent 主循环，不包括意图、改写、重排；留出运行当时预算失败路径还漏报了已用 token，之后补齐并增加回归测试。因此不引用该报告的 token 中位数做成本结论，原始记录不回写伪造数据。

业务评分检查：HTTP 完成状态、审批前无副作用、冻结参数、批准/拒绝后的数据库状态、重复决定结果一致、必要工具或引用。只咨询/缺信息/拒绝场景检查不产生审批；这不等于完整回答质量评测。所有失败保留在分母。执行结果不是由模型说“完成”判定。

[Judge 补充报告](../evaluation/reports/application_judge_v2.json)仅抽取开发报告固定前 6 次轨迹，提供对话、工具证据与业务断言，业务判定不被 Judge 分数覆盖。同模型家族裁判、未对照独立人工评分，不能作为准确率证明。

## 记忆与可靠性

[记忆对照](../evaluation/reports/memory_ablation_v2.json)每模式四项上下文检查：窗口截断后保留纠正的员工号、跨会话召回、跨用户隔离、意图隔离。working/state/full 分别通过 2/4、3/4、4/4。没有调用模型，证明的是上下文装配能力，不是对话任务成功率。

审批动作、参数与用户/请求标识冻结并计算哈希；API 审批必须携带该哈希。批准决定独立持久化，执行时本地业务变更和 operation_id 同事务提交。已批准但未执行、或明确回滚失败，可调用 execute 重试。测试覆盖：

- 20 个并发批准请求仅一个操作记录；批准/拒绝竞争只接受一个决定。
- 一批动作中后续员工不存在时，前面已做的变更同事务回滚。
- 子进程在修改业务状态但尚未提交时 `os._exit`，重启后恢复执行，密码版本只增长一次。
- 同一 chat 请求并发抵达只写一轮记忆；最终回执在会话锁内提交。
- 审批已保存、聊天回执尚未提交时注入中断，重试复用相同审批且不重复写业务状态；聊天阶段不再保留会卡住恢复的旧“结果未知”回执。
- 依赖步骤得到前序证据；失败依赖阻断；预算停止保留已执行 trace 与 token。

[监控故障注入](../evaluation/reports/recovery_v2.json)：10 次本地模型替身失败触发冷却，Triage 接手，推进冷却时钟后同时发起两个请求，只有一个探针进入专职 Agent，成功后恢复健康。四项检查通过。不是实测生产 MTTR。

## 尚未完成的生产能力

企业 SSO、身份绑定的部门授权、外部 IAM/ITSM 执行与对账、跨节点业务数据库、模型原生 token 流、大规模真实工单评测均未实现。本地 SQLite 事务保证不得被描述为外部系统 exactly-once。SSE 仍先等待整轮处理，再发状态和正文分片。

## 本地验证记录

本轮后端 160 项测试通过（38 条 Chroma 依赖弃用警告），前端 14 项测试通过且 Vite 构建成功；v1 离线门禁通过。浏览器完成查看冻结参数、批准及执行状态展示，并通过业务查询确认权限写入。测试数证明回归覆盖，不代替上述模型实验。
