# 原始证据（按需读取）

日常结论与数据见 [evaluation](../evaluation.md)。历史过程文档已移除；原始实验数据保留压缩形式，不作为 AI 默认上下文或当前待办。

| 包 | 内容 | 位置 |
|---|---|---|
| versioned-history | 已跟踪历史基线与配置对照 | `evaluation/evidence/versioned/` |
| masking | 脱敏最终/首次失败、逐条预测、源码和复现材料 | `evaluation/evidence/local/` |
| acceptance | 验收原始数据和数据库修复前失败 | 同上 |
| application-history | 业务、长对话、检索数据 | 同上 |
| masking-sources | 历史配对实验的源码快照，恢复后作为 --source-root 使用 | 同上 |
| invalid-experiments | 台账污染等无效实验，仅用于解释排除原因 | 同上 |

历史报告及源码快照的包内成员与去重别名通过 SHA-256 校验。[压缩索引](2026-11-16-file-index.json.gz) 保存原路径、哈希、包成员和历史清理决定；原有记录保留，新增源码快照位置。11 项评测固定输入由 [独立清单](../../evaluation/fixtures/manifest.json) 管理，不从历史报告目录选样。

```bash
python scripts/evidence.py verify
python scripts/evidence.py extract masking
python scripts/evidence.py extract masking-sources
python scripts/evidence.py extract acceptance --destination output/evidence/acceptance-review
```

只恢复到全新目录，保留原 `evaluation/reports/...` 层级，拒绝越界路径与包内链接。源码快照恢复在 `output/evidence/masking-sources/evaluation/reports/model_masking/source_snapshots/<快照名>/`；历史评测可将该目录传给 `--source-root`。旧绝对路径/命令/源码哈希属于当时测量；不能解释成今天重测过。

`local/` 证据与输入不随 Git 克隆，换机器时一并复制并校验。`clean` 不删除固定输入或证据包；新试跑由 `scripts/dev.py evaluate` 写入临时目录，关键基线/失败选定后再长期保存。
