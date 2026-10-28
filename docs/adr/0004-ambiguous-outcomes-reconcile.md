# 0004 外部调用三分类；结果未知先回查再决定

日期：2026-10-28 · 状态：已采纳

## 背景

调用外部系统超时或拿到 502 时，请求可能已经生效。盲目重试会重复授权（或在非幂等系统上重复建单），直接判失败又会触发错误的补偿。

## 决定

适配器契约（`adapters/base.py`）：`preflight`、`current_level`、`apply(action_id, …)`、`observe(action_id, …)`；错误分三类：

| 类别 | 例子 | worker 行为 |
|---|---|---|
| `PermanentError` | 用户不存在、凭据被拒、4xx 业务拒绝 | 失败，触发补偿，不重试 |
| `TransientError` | 连接被拒、连接超时（请求未发出） | 退避重试，超过上限判失败 |
| `AmbiguousError` | 读超时、传输中断、5xx | 置 `unknown`；下次认领先 `observe`，已生效记成功（`reconciled`），未生效才重做；持续未知进入 `manual_review` |

- `apply` 后再用独立读取 `observe` 一次；看不到目标状态按 `AmbiguousError` 处理，不当作成功。
- 授权前把 baseline 持久化到动作结果，崩溃重试时不会把自己的授权误认成原有权限。
- 回收前比较当前权限；与我们授予的级别不一致（被他人修改）则停止并标记 `drifted`。

## 后果

- Gitea 无幂等键：依靠 PUT/DELETE 的目标状态语义 + `observe`。支持幂等键的系统应透传 action id。
- 本地目录适配器把效果与 action id 同事务提交，`observe` 精确。
- 管理员可调用 `/approvals/{id}/reconcile` 把 `manual_review` 动作重新交给 worker，仍然先回查。
