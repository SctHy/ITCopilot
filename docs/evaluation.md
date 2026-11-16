# 评测结论与数据

工程检查以最近一次 `scripts/dev.py check` 的源码指纹、命令和结果为准；以下均为 **2026-11-12～14 的历史测量**，不代表当前版本重新验收。没有真实用户或生产效果证明。

## 保留结论

| 项目 | 测量结果 | 口径与限制 |
|---|---|---|
| 确定性安全 | 22/22，未授权读/写各 0 | 2026-11-13，脚本化模型，非真实攻击面全集 |
| 真实进程故障 | 24/24 | 独立进程与测试台账；不等同生产基础设施 |
| 应用业务 | strata 182/192，hard 130/138，合计 312/330 | 两套自编场景各三遍；含全部失败，单模型 |
| 记忆治理 | 确定性 20/20；working/state/full 各 84 轮，回忆各 19/19，写会话各 3/4 | 固定会话，不证明生产收益或因果改善 |
| 模型角色 | intent 15/15、summary 12/12、profile 3/9、rerank 12/12、judge 12/12 | 辅助烟测；画像未达标；单候选相对检查不证明绝对质量 |
| 单 Agent 选择 | 42 场景：bounded 41/42、bounded_all 42/42、通用 40/42；多 Agent token 约多 60% | 未观察到显著效果优势，当前保留单 Agent |
| 延迟目标 | 流水线阶段 P99 8.552 → 8.239 s（−3.65%）；Agent P99 5.820 → 5.954 s | 每组 330 用例/489 回合；未达到下降 30% 目标，非完整 HTTP 时延 |
| 已知 SDK 成本 | 每成功用例 $0.00072809 → $0.00072238 | 历史峰值价估算；排除 Jev/embedding，不是账单 |

性能基线与当前快照还包含其他改动，外部负载/缓存/随机性未消除，不能把差值全部归因于截止时间。认证失败和跨用例台账污染的实验不纳入有效成绩。灰度、双人复标及强制线上样本门禁已移除；线上回归集仍为空，不以合成夹具冒充。

## 专题结果

- [公开检索与注入基准](evaluation-public.md)：WixQA、AgentDojo。
- [业务、记忆与多轮指标](evaluation-deep.md)：单次结果、pass^k、未达标项。
- [出站脱敏](model-masking-acceptance.md)：最终配对结果、误改动与已知漏检。
- 常用机器结果：[安全](../evaluation/reports/security_v3.json)、[故障](../evaluation/reports/long_tasks_worker_kill_2026-11-12.json)、[脱敏汇总](../evaluation/reports/model_masking/v3_verified/summary.json)、[保护回退](../evaluation/reports/model_masking/v3_verified/lost_coverage_audit.json)。

## 复现入口

```bash
python scripts/dev.py check --profile backend
python scripts/dev.py evaluate scripts/audit_resume_metrics.py
python scripts/dev.py evaluate scripts/summarize_agent_metrics.py
python scripts/dev.py evaluate --file scripts/evaluate_memory_governance.py
python scripts/dev.py evaluate scripts/evaluate_application.py --limit 1000
python scripts/evaluate_model_route.py --route agent_read --model deepseek-flash --repeats 3
python scripts/check_model_routes.py
```

前两个指标脚本读取冻结证据，不调用模型，不代表当前重测。其他脚本按自身配置可能调用真实模型：明确数据集、模型、次数、预算，并使用独立数据库/Redis/测试身份。完整应用评测显式 `--limit 1000`，不要把前若干条冒充全量。

[固定输入清单](../evaluation/fixtures/manifest.json) 校验 SHA-256；输入缺失/损坏直接失败，不能回退小样本或静默重采。原始报告、失败和源码指纹在 [压缩证据](archive/README.md)，需追溯才恢复。新运行通过 `scripts/dev.py evaluate` 留在忽略目录，重要基线另固定，临时产物按 30 天规则清理。
