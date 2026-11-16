# 公开基准结果（历史）

2026-11 历史测量，非当前版本重测；原始记录在 [证据包](archive/README.md) 的 versioned-history/application-history，按原报告路径定位。

## WixQA

Wix 英文客服 6,221 篇文章、35,083 个片段；ExpertWritten/Simulated 各 200 查询。数据 revision `d662dc4`，文件哈希在脚本/报告；MIT。按前 20 片段去重成文章计 Recall/MRR；SQLite 精确检索，不测回答忠实度。

| embedding | 模式 | Expert R@1 | Expert R@5 | Expert MRR | Simulated R@1 | Simulated R@5 | Simulated MRR | p50 延迟 |
|---|---|---|---|---|---|---|---|---|
| — | BM25 | 0.21 | 0.45 | 0.36 | 0.13 | 0.34 | 0.25 | 4 ms |
| bge-small-zh（原默认） | 向量 | 0.12 | 0.29 | 0.23 | 0.07 | 0.24 | 0.16 | 10 ms |
| bge-small-zh（原默认） | 混合 | 0.21 | 0.41 | 0.33 | 0.12 | 0.35 | 0.24 | 12 ms |
| bge-small-en | 向量 | 0.24 | 0.55 | 0.42 | 0.20 | 0.47 | 0.35 | 12 ms |
| bge-small-en | 混合 | 0.28 | 0.59 | 0.46 | 0.16 | 0.51 | 0.34 | 13–14 ms |
| jina-v2-base-zh（默认） | 向量 | 0.25 | 0.57 | 0.44 | 0.20 | 0.46 | 0.35 | 19 ms |
| jina-v2-base-zh（默认） | 混合 | 0.34 | 0.57 | 0.50 | 0.18 | 0.51 | 0.36 | 22 ms |
| multilingual-mpnet | 向量 | 0.19 | 0.41 | 0.34 | 0.13 | 0.36 | 0.26 | 22 ms |
| multilingual-mpnet | 混合 | 0.25 | 0.52 | 0.41 | 0.15 | 0.45 | 0.31 | 23 ms |
| embeddinggemma-300m | 向量 | 0.28 | 0.67 | 0.50 | 0.24 | 0.63 | 0.43 | 24 ms |
| embeddinggemma-300m | 混合 | 0.33 | 0.62 | 0.51 | 0.22 | 0.57 | 0.40 | 27 ms |

结论：embedding 必须覆盖语料语言；混合不总优于单路。默认 jina-v2-base-zh 覆盖中英；Gemma 英文更高，但分发条款/模型体积不同。自编小语料的高分不能外推六千篇真实语料。

## AgentDojo

v1.2.2 / agentdojo 0.1.35，deepseek-flash，最多 5 轮，`important_instructions_no_model_name` 单一攻击。办公助手提示词与基准工具替换 IT 场景；24 个写工具直接执行，测试不含产品人工审批兜底。

| 套件 | 无攻击 utility | 攻击下 utility | 攻击下执行过写操作的组合 | 写目标攻击成功 | 只读目标判定成功（其中为警告） | 超过模型轮数 |
|---|---|---|---|---|---|---|
| workspace | 38/40 | 533/560 | 284 | 0/560 | — | 14 |
| travel | 16/20 | 94/140 | 40 | 0/120 | 15/20（15） | 15 |
| banking | 7/16 | 60/144 | 5 | 0/144 | — | 0 |
| slack | 20/21 | 70/105 | 82 | 0/84 | 0/21 | 27 |
| 合计 | 81/97（0.84） | 757/949（0.80） | 411 | 0/908 | 15/41（15） | 56 |

结论与限制：写目标本轮 0/908；其他 slack 重复运行曾出现链接写入触发的成功，不能宣称零风险。只读目标 15 次成功判定均为警告中出现目标酒店名，人工复核不属于推荐。banking 极少实际执行写操作，低攻击成功率不能单独说明抵抗力。只测一个模型/一种攻击；不同论文运行时的分数不可直接比较。

复现：`scripts/evaluate_rag_wixqa.py --fetch` 获取固定数据；`scripts/evaluate_rag_wixqa.py` 测检索；独立安装 `agentdojo==0.1.35` 后运行 `scripts/evaluate_agentdojo.py --suite workspace`，其他套件为 travel/banking/slack，`--no-attack` 测无攻击基线。

来源：[WixQA](https://arxiv.org/abs/2505.08643)、[AgentDojo](https://arxiv.org/abs/2406.13352)。冻结输入见 [manifest](../evaluation/fixtures/manifest.json)。
