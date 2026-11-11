// Presentation only. The API independently checks identity, quorum and frozen hash.
export function approvalPermissions(approval, identity) {
  const roles = identity?.roles || []
  const reviewer = roles.some(role => role === 'approver' || role === 'it_admin')
  const ownRequest = !!identity && approval?.requester === identity.subject
  const ownTarget = !!identity?.employee_id && (approval?.action_requests || []).some(
    action => String(action.arguments?.employee_id) === String(identity.employee_id))
  const voted = !!identity && (approval?.decisions || []).some(vote => vote.actor === identity.subject)
  const assigned = !approval?.assigned_reviewers?.length || approval.assigned_reviewers.includes(identity?.subject)
  const writable = approval?.room_can_write !== false
  const pending = approval?.status === 'pending'
  // A revocation still waiting for its time is an active grant that can be given back early.
  const now = Date.now()
  const releasable = approval?.status === 'approved' && (approval?.revocations || []).some(
    step => step.status === 'queued' && (!step.not_before || Date.parse(step.not_before) > now))
  const reason = !writable ? '观察者不能审批。' : !assigned ? '等待指定审批人处理。' : !reviewer ? '等待审批人处理。' : ownRequest ? '申请人不能审批自己的申请。'
    : ownTarget ? '不能审批针对本人账号的变更。' : voted ? '你已提交审批意见，等待其他审批人或执行结果。' : ''
  return { canDecide: pending && writable && assigned && reviewer && !ownRequest && !ownTarget && !voted,
    canClarify: pending && writable && reviewer && !ownRequest, canReconcile: roles.includes('it_admin'),
    canWithdraw: pending && ownRequest,
    canRelease: releasable && (ownRequest || ownTarget || roles.includes('it_admin')), reason }
}
