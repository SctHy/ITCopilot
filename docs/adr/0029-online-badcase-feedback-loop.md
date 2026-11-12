# ADR 0029：线上 badcase 回流与标注闭环

状态：已采纳

## 背景

Redis trace 七天后过期，线上失败没有用户反馈、人工归因及回归门禁之间的可追溯通路。只保存聚合成功率无法说明错在意图、检索、工具还是产品能力。

## 决定

迁移 0008 添加租户隔离的 `feedback`、`badcases` 与平台挖掘游标；回合元数据增加 `quality` JSON，保存澄清、知识证据、写操作回合及声明纠正等布尔事实，避免从抽样样本推断总体指标。旧回合 quality 一次性回填空对象，相关指标明确排除未采集的分母。

反馈写入只允许私人回合所有者，跨租户及其他人即使是管理员也不能代评。共享事项回复允许当前成员（包括观察者）反馈，先重新检查事项访问权，再绑定该事项 Agent 回复的运行 ID。重复反馈按 `(request_id, actor)` 覆盖，备注在写入前统一脱敏。

API 中的内嵌 worker 和独立 execution worker 均每十分钟挖掘：自动信号、负反馈，以及成功回合的 1% 稳定伪随机抽样。游标持久保存并重叠扫描一个调度周期，以覆盖创建元数据与实际提交之间的时间差；业务审批、事项状态变更会重新关联旧回合。唯一 request_id 消除重复样本。工作记忆中的三次连续缺槽位和六十秒内相似重述在正文仍可读取时挖掘。

快照复制已脱敏 trace、问题与回答；正文仍缺失时标记 snapshot_partial，允许归因和排除，禁止导出为可执行用例。共享事项从自己的持久讨论取正文，不访问私人记忆。独立 worker 使用只读 Redis trace 适配器，不构造模型客户端。

样本状态由 `Machine` 声明：new → triaged → labeled → promoted，前三态可 dismissed。标注使用固定根因表、严重程度及现有 application 评测字段（tools、forbid_tools、outcome、forbid_text、evidence、intent）；保存首标时原子通过 triaged 进入 labeled。成功抽样样本再用独立哈希抽取 10% 要求不同主体复标，避免与 1% 抽样分桶相关。提升要求正文完整、存在可验证期望、复标完成；终态不允许重写。

导出脚本只读 promoted 样本，把已知身份、手机号、密钥、邮箱等去除并把工号映射为演示目录 1001；工具期望转为现有评测器的 evidence 约束。输出须经人工审阅后提交；脚本不自动提交。初始 online_regression 数据集为空，实际线上样本不能用合成样本冒充。确定性 CI 的样本须另经审阅标记可复现的 fixture；真实模型发布评测仍独立运行三次。

## 后果与限制

质量报告覆盖正确性、过程、体验、成本、稳定性，并保留每个比例的分子/分母。未采集成本、空分母、无双标等结果为 NULL。成功事项以独立目标核验 passed 为准，不能使用模型声称完成代替。模型轮数、首字、整轮时延来自持久记录；claim correction 分别统计频率、独立阶段开销和有/无纠正的观察组时延差，后者不作因果解释。

`unauthorized_writes` 独立扫描已成功 apply 的审批与投票授权证据；它是观察到的数据库契约违例计数，不能代替安全回归攻击评测。无真实线上流量时不能报告真实漏检率、满意度或模型效果。姓名与任意自由文本的完整匿名化仍需要导出后的人工审阅。

## 验证方法

```sh
.venv/bin/pytest -q tests/test_badcases.py tests/test_online_regressions.py tests/test_tenant_isolation.py
IT_COPILOT_TEST_DATABASE_URL=postgresql+psycopg://... .venv/bin/pytest -q tests/test_badcases.py tests/test_tenant_isolation.py
IT_COPILOT_DATABASE_URL=... .venv/bin/python scripts/export_badcases.py --status promoted --output evaluation/datasets/online_regression.json
IT_COPILOT_DATABASE_URL=... .venv/bin/python scripts/quality_report.py --since 2026-11-01T00:00+00:00 --until 2026-11-12T00:00+00:00
.venv/bin/python scripts/evaluate_application.py --dataset online_regression --repeats 3
npm --prefix frontend run typecheck
npm --prefix frontend test
npm --prefix frontend run build
```

测试覆盖负反馈覆盖写入、租户与所有者拒绝、观察者反馈、信号快照脱敏、快照过期、双人标注和提升、重述/缺槽位、导出用例经 FunctionModel 的真实 HTTP 链执行、抽样哈希独立性。确定性链路报告见 `evaluation/reports/badcase_pipeline_20261112.json`。真实模型 online_regression 评测未运行；暂无经审阅的真实线上样本。
