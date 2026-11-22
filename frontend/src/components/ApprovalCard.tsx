// One approval, its frozen parameters, execution steps and the actions the viewer may take.
// Action availability is a display hint (lib/approvalActions.ts); the API enforces identity, quorum and hash.
import { RefreshCwIcon, ShieldCheckIcon } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Separator } from '@/components/ui/separator'
import { Spinner } from '@/components/ui/spinner'
import { approvalPermissions } from '../lib/approvalActions.ts'
import type { Approval, ApprovalDecision, Identity } from '../lib/types.ts'
import { Collapse, ErrorAlert, Facts, Mono, StatusChip, type Tone } from './ui.tsx'

const actionLabels: Record<string, string> = { request_access: '申请权限', reset_password: '重置密码', unlock_account: '解锁账号', create_ticket: '创建工单', revoke_access: '回收权限', lock_device: '远程锁定设备' }
const fieldLabels: Record<string, string> = { employee_id: '工号', system: '系统', role: '角色', reason: '理由', title: '标题', description: '描述', priority: '优先级', duration_hours: '有效期（小时）', device_id: '设备' }
const statusLabels: Record<string, string> = { draft: '待申请人确认', pending: '待审批', approved: '已批准', rejected: '已拒绝', expired: '已过期', superseded: '已被替代', invalidated: '策略变更已失效', withdrawn: '申请人已撤回' }
const statusTones: Record<string, Tone> = { draft: 'accent', pending: 'warning', approved: 'success', rejected: 'danger', expired: 'default', superseded: 'default', invalidated: 'danger', withdrawn: 'default' }
const decisionLabels: Record<string, string> = { approved: '批准', rejected: '拒绝' }
const executionLabels: Record<string, string> = { not_executed: '尚未执行', queued: '排队中', executing: '执行中', succeeded: '执行成功', failed: '执行失败（请查看回收状态）', manual_review: '需人工复核' }
const executionTones: Record<string, Tone> = { queued: 'accent', executing: 'accent', succeeded: 'success', failed: 'danger', manual_review: 'warning' }
const lifecycleLabels: Record<string, string> = { compensating: '正在补偿回收', active_until_revocation: '权限生效中，已安排回收', revoking: '正在回收权限', manual_review: '业务流程需人工复核', expiry_due: '已到期，等待过期扫描' }
const actionStatus: Record<string, string> = { queued: '排队中', executing: '执行中', unknown: '结果未知，待回查', succeeded: '成功', failed: '失败', cancelled: '已取消', manual_review: '需人工复核' }
const stepTones: Record<string, Tone> = { succeeded: 'success', failed: 'danger', unknown: 'warning', manual_review: 'warning', queued: 'accent', executing: 'accent' }

function formatTime(iso?: string | null) {
  return iso ? new Date(iso).toLocaleString('zh-CN', { hour12: false }) : ''
}

export type ApprovalAction = (decision?: ApprovalDecision, note?: string) => void

export function ApprovalCard({ approval, busy = false, error = '', identity, allowClarification = false, onAction }: {
  approval?: Approval | null; busy?: boolean; error?: string; identity?: Identity | null; allowClarification?: boolean; onAction: ApprovalAction
}) {
  return (
    <div className="mt-3 flex flex-col gap-2.5 rounded-xl border bg-card p-3.5 text-card-foreground" data-state={busy ? 'loading' : error ? 'failed' : approval ? 'ready' : 'unloaded'}>
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <span className="grid size-7 place-items-center rounded-lg bg-accent text-accent-foreground"><ShieldCheckIcon className="size-4" /></span>
          <strong className="text-sm">操作审批</strong>
          {approval && <StatusChip tone={statusTones[approval.status] || 'default'}>{statusLabels[approval.status] || approval.status}</StatusChip>}
        </div>
        {busy && <Spinner className="size-4" />}
      </div>
      {!approval ? (
        <Button type="button" size="sm" variant="secondary" disabled={busy} onClick={() => onAction()}>查看申请内容</Button>
      ) : (
        <ApprovalBody approval={approval} identity={identity ?? null} busy={busy} allowClarification={allowClarification} onAction={onAction} />
      )}
      {error && <ErrorAlert>{error}</ErrorAlert>}
    </div>
  )
}

function ApprovalBody({ approval, identity, busy, allowClarification, onAction }: {
  approval: Approval; identity: Identity | null; busy: boolean; allowClarification: boolean; onAction: ApprovalAction
}) {
  const meta = [
    `申请人 ${approval.requester_name || approval.requester || '-'}`,
    `策略 ${approval.policy_version || '-'}`,
    `需 ${approval.approvals_required ?? '-'} 人批准`,
    approval.expires_at && approval.status === 'pending' ? `${formatTime(approval.expires_at)} 前有效` : '',
    approval.expires_at && approval.status === 'draft' ? `请在 ${formatTime(approval.expires_at)} 前确认` : ''
  ].filter(Boolean)
  const votes = (approval.decisions || []).map(d => `${d.actor_name || d.actor} ${decisionLabels[d.decision] ?? d.decision}`).join('，')
  return (
    <>
      <p className="text-[13px] leading-relaxed text-muted-foreground">{meta.join(' · ')}</p>
      {!!approval.conflicts?.length && <p className="text-[13px] text-warning-foreground">同一资源还有 {approval.conflicts.length} 个在途申请</p>}
      {(approval.action_requests || []).map((action, index) => (
        <div key={index} className="rounded-md bg-card-secondary px-3 py-2.5">
          <p className="mb-1.5 text-[13px] font-medium">{actionLabels[action.tool_name] || action.tool_name}</p>
          <Facts items={Object.entries(action.arguments || {}).map(([key, value]) => [fieldLabels[key] || key, String(value)])} />
        </div>
      ))}
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="text-muted-foreground">执行</span>
        <StatusChip tone={executionTones[approval.execution ?? ''] || 'default'}>{executionLabels[approval.execution ?? ''] || approval.execution || '—'}</StatusChip>
        {lifecycleLabels[approval.lifecycle ?? ''] && <StatusChip tone="accent">{lifecycleLabels[approval.lifecycle ?? '']}</StatusChip>}
        {votes && <span className="text-[13px] text-muted-foreground">审批意见：{votes}</span>}
      </div>
      {(!!approval.results?.length || !!approval.revocations?.length) && (
        <ul className="grid gap-1.5 text-[13px]">
          {(approval.results || []).map(step => (
            <li key={step.operation_id} className="flex flex-wrap items-center gap-1.5">
              <StatusChip tone={stepTones[step.status] || 'default'}>{actionStatus[step.status] || step.status}</StatusChip>
              <span>{actionLabels[step.action ?? ''] || step.action}</span>
              {step.adapter !== 'local' && step.adapter && <span className="text-muted-foreground">· {step.adapter}</span>}
              {step.preexisting && <span className="text-muted-foreground">· 已有同等权限，未变更</span>}
              {step.covered_by && <span className="text-muted-foreground">· 由其他有效授权覆盖，已独立安排回收</span>}
              {step.reconciled && <span className="text-muted-foreground">· 经回查确认</span>}
              {step.error && <span className="text-destructive-foreground">· {step.error}</span>}
            </li>
          ))}
          {(approval.revocations || []).map(step => (
            <li key={step.operation_id} className="flex flex-wrap items-center gap-1.5">
              <StatusChip tone={stepTones[step.status] || 'default'}>{actionStatus[step.status] || step.status}</StatusChip>
              <span>到期回收</span>
              {step.status === 'queued' && <span className="text-muted-foreground">· 计划于 {formatTime(step.not_before)}</span>}
              {step.error && <span className="text-destructive-foreground">· {step.error}</span>}
            </li>
          ))}
        </ul>
      )}
      <Separator />
      <ApprovalActions approval={approval} identity={identity} busy={busy} allowClarification={allowClarification} onAction={onAction} />
      {!!approval.results?.length && (
        <Collapse trigger="查看执行凭据" triggerClassName="-ml-2" panelClassName="pt-2"><Mono>{JSON.stringify(approval.results, null, 2)}</Mono></Collapse>
      )}
    </>
  )
}

function ApprovalActions({ approval, identity, busy, allowClarification, onAction }: {
  approval: Approval; identity: Identity | null; busy: boolean; allowClarification: boolean; onAction: ApprovalAction
}) {
  const permissions = approvalPermissions(approval, identity)
  const steps = [...(approval.results || []), ...(approval.revocations || [])]
  const reviews = steps.filter(step => ['manual_review', 'unknown'].includes(step.status) || step.drifted)
  const confirmThen = (action: ApprovalDecision, message: string) => { if (window.confirm(message)) onAction(action) }
  const choices = [
    ['retry', '重新入队', '执行器将先回查，再决定是否重试。'],
    ['accept_external', '采纳外部实态', '只读核实目标系统并更新台账。'],
    ['mark_resolved', '线下结案', '确认已在线下处理完毕。'],
  ] as const
  return (
    <div className="flex flex-col gap-2" role="group" aria-label="审批选项">
      {approval.status === 'draft' && <p className="text-[13px] text-muted-foreground">这是 Agent 起草的申请，核对上面的内容无误后再提交；提交后才会交给审批人。</p>}
      <div className="flex flex-wrap items-center gap-2">
        {approval.status === 'draft' && permissions.canConfirm && <Button type="button" size="sm" disabled={busy} onClick={() => onAction('confirm')}>确认提交</Button>}
        {approval.status === 'draft' && permissions.canWithdraw && <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => onAction('withdraw')}>取消申请</Button>}
        {approval.status === 'pending' && permissions.canDecide && (
          <>
            <Button type="button" size="sm" disabled={busy} onClick={() => onAction('approved')}>同意审批</Button>
            <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => onAction('rejected')}>拒绝申请</Button>
          </>
        )}
        {approval.status === 'pending' && allowClarification && permissions.canClarify && (
          <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => onAction('clarify')}>要求补充说明</Button>
        )}
        {approval.status === 'pending' && permissions.canWithdraw && (
          <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => confirmThen('withdraw', '确认撤回这条申请？撤回后不会再执行，如仍需要请重新提交。')}>撤回申请</Button>
        )}
        {permissions.canRelease && (
          <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => confirmThen('release', '确认现在归还这项限时权限？归还后如需使用要重新申请。')}>提前归还权限</Button>
        )}
        {permissions.canReconcile && reviews.map(step => <fieldset key={step.operation_id}>
          <legend>{actionLabels[step.action || ''] || step.action} · {step.operation_id.slice(0, 8)}</legend>
          {choices.filter(([mode]) => step.status !== 'cancelled' || mode !== 'retry').map(([resolution, label, hint]) => <Button key={resolution} type="button" size="sm" variant="outline"
            disabled={busy || step.version === undefined} onClick={() => {
              const reason = window.prompt(hint + ' 请填写处置原因（进入审计）。')?.trim()
              if (reason && step.version !== undefined) onAction({resolution, operation_id: step.operation_id, version: step.version, reason})
            }}>{label}</Button>)}
        </fieldset>)}
        {permissions.canReconcile && approval.audit?.filter(entry => entry.detail?.reason).map((entry, index) => <p key={index}>{entry.actor} · {entry.event} · {entry.detail?.reason}</p>)}
        <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => onAction()}><RefreshCwIcon className="size-3.5" />刷新状态</Button>
      </div>
      {approval.status === 'draft' && permissions.reason && <p className="text-[13px] text-muted-foreground">{permissions.reason}</p>}
      {approval.status === 'pending' && permissions.reason && !permissions.canWithdraw && <p className="text-[13px] text-muted-foreground">{permissions.reason}</p>}
    </div>
  )
}
