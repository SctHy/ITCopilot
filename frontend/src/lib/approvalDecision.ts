// Apply an approval-card action. The card stays bound to the connection that produced it.
import { backendMeta } from '../api/http.ts'
import { confirmApproval, reconcileApproval, releaseApproval, requestApproval, resolveApproval, withdrawApproval } from '../api/approvals.ts'
import type { ApprovalDecision, ApprovalItem, Settings } from './types.ts'

/** Loads (no decision) or acts on `item`, mutating its approval/busy/error fields in place. */
export async function applyApprovalAction(item: ApprovalItem, current: Settings, decision?: ApprovalDecision, note = ''): Promise<void> {
  if (item.approvalBusy) return
  item.approvalBusy = true
  item.approvalError = ''
  try {
    // Keep the original endpoint: switching connections must never approve an unrelated request.
    if (backendMeta(item.approvalBackend, current).baseUrl !== backendMeta(item.approvalBackend, item.approvalSettings).baseUrl) {
      throw new Error('请切回生成此申请的服务连接后再审批。')
    }
    // 以当前登录身份操作；审批权限与职责分离由服务端判定。
    const target = { ...item.approvalSettings, accessToken: current.accessToken }
    const actions: Record<string, () => Promise<ApprovalItem['approval']>> = {
      reconcile: () => reconcileApproval(item.approvalBackend, target, item.approvalId),
      confirm: () => confirmApproval(item.approvalBackend, target, item.approvalId, item.approval?.payload_hash),
      withdraw: () => withdrawApproval(item.approvalBackend, target, item.approvalId),
      release: () => releaseApproval(item.approvalBackend, target, item.approvalId),
      resolve: () => resolveApproval(item.approvalBackend, target, item.approvalId, note),
    }
    const action = decision ? actions[decision] : undefined
    item.approval = action
      ? await action()
      : await requestApproval(item.approvalBackend, target, item.approvalId, decision, item.approval?.payload_hash)
  } catch (error) {
    item.approvalError = error instanceof Error ? error.message : String(error)
  } finally {
    item.approvalBusy = false
  }
}
