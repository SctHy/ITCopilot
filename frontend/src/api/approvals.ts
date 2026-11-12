// Approval endpoints. Permission rules live on the server; see lib/approvalActions.ts for display hints.
import { jsonBody, requestFor } from './http.ts'
import type { Approval, Settings } from '../lib/types.ts'

const path = (id: string, suffix = '') => `/approvals/${encodeURIComponent(id)}${suffix}`

/** Without a decision this reads the approval; with one it votes against the frozen payload hash. */
export function requestApproval(type: string, settings: Settings, id: string, decision?: string, hash?: string) {
  return requestFor<Approval>(type, settings, path(id), decision ? jsonBody({ decision, payload_hash: hash }) : {})
}

// 人工对账（IT 管理员）：结果未知或需复核的动作重新交给执行器，先回查目标系统再决定是否重做。
export const reconcileApproval = (type: string, settings: Settings, id: string) =>
  requestFor<Approval>(type, settings, path(id, '/reconcile'), { method: 'POST' })

// 人工结案（IT 管理员）：目标系统已人工处理后结束需复核的动作，释放该资源上的后续申请。
export const resolveApproval = (type: string, settings: Settings, id: string, note: string) =>
  requestFor<Approval>(type, settings, path(id, '/resolve'), jsonBody({ note }))

// 申请人确认 Agent 起草的申请（ADR 0020）；哈希绑定卡片上展示的冻结内容，确认后才进入审批人队列。
export const confirmApproval = (type: string, settings: Settings, id: string, hash?: string) =>
  requestFor<Approval>(type, settings, path(id, '/confirm'), jsonBody({ payload_hash: hash }))

// 申请人撤回仍待审批的申请或取消未确认的草稿；已有结果的申请服务端返回 409。
export const withdrawApproval = (type: string, settings: Settings, id: string, reason = '') =>
  requestFor<Approval>(type, settings, path(id, '/withdraw'), jsonBody({ reason }))

// 提前归还该申请开通的限时权限：到期回收立即执行，仍做漂移检查。
export const releaseApproval = (type: string, settings: Settings, id: string) =>
  requestFor<Approval>(type, settings, path(id, '/release'), { method: 'POST' })

export const listApprovals = (type: string, settings: Settings, status = 'pending') =>
  requestFor<{ items: Approval[] }>(type, settings, `/approvals?status=${encodeURIComponent(status)}`)
