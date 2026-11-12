# ADR 0032：长期记忆治理：写入侧脱敏、准入、去重，召回侧衰减

状态：已采纳

## 背景

把用户原文直接向量化，会让密码、手机号等信息进入长期存储。澄清、失败和无证据回答也不能作为下次接待的事实；重复问答与陈旧画像会挤占上下文。权限、工单和设备状态仍由当前工具查询提供权威依据。

## 决定

- `core.redaction.redact_text` 统一处理用户给出的密码/验证码、API key、Bearer、手机号、身份证及通过 Luhn 校验的银行卡号。工号、资产号保留。工作记忆、状态、摘要输入输出、画像输入输出、情景文档和 trace 字符串均经过该边界。Redis 回执以消息 SHA256 校验重试，避免保留敏感消息原文。
- `memory.admission.admit_turn` 根据成功状态、意图、缺槽位与工具/检索证据决定准入，重要度由审批/工单/知识证据决定；只保存问题与回答各 300 字的脱敏摘要和业务记录 ID。
- 情景记忆写入在主体级 Redis 租约中查询同用户最近邻，距离小于 0.08 时更新原行、刷新时间并累计 `seen`。同一 request 重放不增加计数；不同主体始终隔离。
- 画像 schema 5 保留四个字段，并用 `field_meta` 记录各自时间及来源 request。字段新值替代旧值；没有重新变化的字段保留其来源时间，超过 180 天不进提示词。旧 schema 转换集中在既有 `_upgrade_profile`，缺少可信写入时间的旧字段视为过期；一次性迁移脚本可用向量表写入时间回填。
- 情景召回使用 `max(ts,last_seen)` 计算衰减，`seen` 对 importance 提供有界加成，并在提示词注明历史可能过时。
- `/memory/me` 提供本人摘要及时间；删除按不可伪造的 scope key 过滤，和 `admin_audit` 同一数据库事务提交。其他人的文档 ID 返回 404。
- `scripts/redact_existing_memory.py` 一次性清理旧文档和元数据，并重新向量化变化文档。上线前必须执行该脚本；本改动未对开发者现有库自动做批量修改。

## 后果

写入多一次最近邻检索和主体级串行化。凭据模式并非通用 DLP，不能识别没有标签的任意密码；已有库和历史 Redis TTL 数据需要迁移/自然到期。近重复阈值沿用计划值，未宣称已完成真实流量调优。用户删除长期记忆不删除审批、执行台账、审计和仍在 TTL 内的幂等回执。

## 验证

```sh
.venv/bin/pytest -q tests/test_memory_governance.py tests/test_memory_efficiency.py tests/test_memory_maintenance.py
.venv/bin/python scripts/evaluate_memory_governance.py --output evaluation/reports/memory_governance_2026-11-12.json
.venv/bin/python scripts/redact_existing_memory.py --database-url sqlite:///data/it_copilot.sqlite3
```

确定性治理评测已运行：20 段对话、20/20 通过（FunctionModel + fakeredis，真实 SQLite/向量存储），报告仅证明写入/准入/画像治理断言。2026-11-13 已运行真实模型 memory ablation：working/state/full 各 4 个固定会话、84 轮，三种模式均通过 19/19 回忆探针、3/4 写操作会话断言。未观察到本轮回忆探针收益下降，但单次相关会话与关键词判分不证明生产效果或治理前后因果改善。报告见 `evaluation/reports/acceptance_2026-11-13/memory_ablation.json`。SQLite/PostgreSQL 完整契约结果见 `docs/improvement-acceptance.md`。
