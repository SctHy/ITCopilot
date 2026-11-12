// Presentation only. The API independently checks identity, quorum and frozen hash.
import type { Approval, Identity } from './types.ts'

export interface ApprovalPermissions {
  canDecide: boolean
  canClarify: boolean
  canReconcile: boolean
  canConfirm: boolean
  canWithdraw: boolean
  canRelease: boolean
  reason: string
}

export function approvalPermissions(approval: Approval | null | undefined, identity: Identity | null | undefined): ApprovalPermissions {
  const roles = identity?.roles || []
  const reviewer = roles.some(role => role === 'approver' || role === 'it_admin')
  const ownRequest = !!identity && approval?.requester === identity.subject
  const ownTarget = !!identity?.employee_id && (approval?.action_requests || []).some(
    action => String(action.arguments?.employee_id) === String(identity.employee_id))
  const voted = !!identity && (approval?.decisions || []).some(vote => vote.actor === identity.subject)
  const assigned = !approval?.assigned_reviewers?.length || approval.assigned_reviewers.includes(identity?.subject ?? '')
  const writable = approval?.room_can_write !== false
  const pending = approval?.status === 'pending'
  // A draft waits on its requester: nobody else acts on it (the server refuses votes on drafts).
  const draft = approval?.status === 'draft'
  // A revocation still waiting for its time is an active grant that can be given back early.
  const now = Date.now()
  const releasable = approval?.status === 'approved' && (approval?.revocations || []).some(
    step => step.status === 'queued' && (!step.not_before || Date.parse(step.not_before) > now))
  const reason = draft ? (ownRequest ? '' : '等待申请人确认提交后，审批人才能处理。') : !writable ? '观察者不能审批。' : !assigned ? '等待指定审批人处理。' : !reviewer ? '等待审批人处理。' : ownRequest ? '申请人不能审批自己的申请。'
    : ownTarget ? '不能审批针对本人账号的变更。' : voted ? '你已提交审批意见，等待其他审批人或执行结果。' : ''
  return { canDecide: pending && writable && assigned && reviewer && !ownRequest && !ownTarget && !voted,
    canClarify: pending && writable && reviewer && !ownRequest, canReconcile: roles.includes('it_admin'),
    canConfirm: draft && ownRequest,
    canWithdraw: (pending || draft) && ownRequest,
    canRelease: releasable && (ownRequest || ownTarget || roles.includes('it_admin')), reason }
}
