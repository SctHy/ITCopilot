# ADR 0030：模型路由：硬约束过滤与评测决定偏好

状态：已采纳

## 背景

全局单模型不能表达调用角色、租户数据等级与地域限制，也无法把供应商故障、延迟和预算纳入选择。将候选交给模型自行选择会削弱合规约束，且当前没有真实流量支持在线学习。

## 决定

- `config/model_routes.yaml` 声明模型凭据环境变量引用、能力、地域、数据等级、价格和按角色排列的候选。Pydantic 在加载时拒绝未知字段、非法数值、悬空引用、缺失工具能力和没有合规候选的租户。未单列租户显式继承 `default` 配置。
- `ModelRouter.pick(route, tenant)` 先过滤合规、地域和熔断，再按配置顺序选择。首选最近五分钟 P95 超出该路由 SLO 时优先健康备选；每个模型复用现有 CircuitBreaker，generation 避免过时的并发结果关闭新熔断。取消释放半开探测。
- 热加载检查文件 mtime，非法新配置抛出错误，成功加载写 `model_route` 变更事件；熔断打开也记录事件以供归因。
- `allowed_writes` 非空或 `None` 走规格的写路由，空集合走只读路由。只有尚未发送任何 `answer_delta` 时，模型不可用才允许切换一次；已发送文本后维持原失败边界。
- 从 `turn_records` 计算租户 UTC 当日模型成本，缓存60秒。达到 `tenants.quotas.daily_cost_usd` 后，写路由隐藏所有副作用工具并说明预算，只读路由继续。确认已有草稿走确定性路径，记录 `model=none / route=confirmation / cost=0`。
- Agent、意图、检索改写/重排、摘要、画像、共享事项进度建议及评测模型均通过 router 取得模型。后台画像任务保留提交者 context，以免平台级 worker 丢失租户而使用错误合规配置。
- 思考档位和轮数只从路由配置读取，移除两个全局 Agent 环境变量。`ModelSettings.timeout` 限制供应商一次尝试；PydanticAI 的 `request_timeout` 则限制含SDK重试及读完流的整个请求。WP6进一步对两者施加整轮剩余时间上限。
- 版本元数据记录最终候选、路由及选择原因；输入缓存按单独价格计费，从普通输入量中扣除。当前峰值估算见官方价格页，非供应商实际账单。
- 当前只配置已有凭据的 `deepseek-flash`。价格于2026-11-12从 [DeepSeek官方价格页](https://api-docs.deepseek.com/quick_start/pricing/) 核验：峰值每百万输入/输出/缓存命中为0.30/1.20/0.006美元，非高峰减半。没有引入第二个真实供应商。

## 候选资格与门禁

```sh
.venv/bin/python scripts/evaluate_model_route.py --route agent_read --model deepseek-flash --repeats 3
.venv/bin/python scripts/evaluate_model_route.py --route summary --model deepseek-flash --repeats 3
.venv/bin/python scripts/check_model_routes.py
```

Agent读/写候选在 `evaluate_application.py` 的应用数据集上运行真实模型及业务oracle。辅助角色不冒充端到端业务成功：summary验证固定事实与ID保留，profile验证字段修订及秘密排除，rewrite/rerank验证关键词覆盖与已知相关项排序，intent对固定严格标签判分，judge对冻结数据库证据及人工标签判分。judge不能覆盖任何应用业务断言。这是对计划中统一应用数据集评测方法的细化，以免不触发某角色时仍得到虚假的资格报告。

每份报告包含实际角色调用数、题集哈希、模型配置哈希、样本量、成功率、Wilson区间、P95和每个成功样本成本。门禁拒绝缺报告、少于3次重复、没有实际角色调用、候选配置变化或不同题集的横向比较；同route候选的Wilson下界须不低于首选下界减3个百分点。报告文件由人审核后提交。

## 后果

健康和P95在单进程内观察，各副本不共享熔断历史；预算统计有最多60秒延迟，并非强一致消费限额。单候选部署不会获得真实供应商故障切换收益；该边界由两个FunctionModel确定性覆盖。辅助角色冻结题规模小，只是资格烟测，不能证明生产质量。BYOK、在线学习和bandit未实现。

## 验证

SQLite路由、流式前后切换、预算隐藏写工具、非法配置与热加载、辅助oracle/门禁测试已通过；PostgreSQL专项（路由与记忆治理合计）39项通过。真实角色报告已运行并写入 `evaluation/reports/routing/`：意图15/15、摘要12/12、画像3/9、改写重排12/12、裁判12/12；读/写旧应用报告因跨用例台账污染已标invalid，等待修复隔离后重跑。画像失败为字段归类与变更后仍保留旧部门，未放宽断言。这个角色烟测prompt比生产画像提示简短，不能外推为生产失败率。当前唯一候选按自身作为相对基线，所以辅助角色的相对比较通过不等于绝对质量已达标；整体门禁须等待有效读/写报告。没有报告的候选必须保持门禁失败，不能用未运行代替通过。
