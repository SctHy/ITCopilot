import { Avatar, AvatarFallback } from '@/components/ui/avatar'
import { ApprovalCard } from '../ApprovalCard.tsx'
import { BotAvatar } from '../ChatMessage.tsx'
import { FeedbackButtons } from '../FeedbackButtons.tsx'
import { MarkdownMessage } from '../MarkdownMessage.tsx'
import { groupTaskEvents } from '../../lib/taskPresentation.ts'
import { useTaskCollaboration } from './TaskCollaboration.tsx'
import { StatusChip } from '../ui.tsx'
import type { Approval, ApprovalDecision, Identity, Settings, TaskEvent, TaskRoom } from '../../lib/types.ts'

const roles: Record<string, string> = { requester: '申请人', approver: '协作者', collaborator: '协作者', assistant: 'Agent', approval: '审批状态', approval_vote: '正式审批决定', system: '系统', progress: '任务进度', todo: '协作待办', todo_answer: '待办答复', todo_changed: '待办调整', reminder: '定时提醒', approval_reminder: '审批提醒', budget: '运行预算', file: '附件材料', member: '成员管理', reviewer_assignment: '审批指派', suggestion: '进度建议' }
const people = new Set(['requester', 'approver', 'collaborator', 'assistant'])
const formatTime = (seconds: number) => new Date(seconds * 1000).toLocaleString('zh-CN', { hour12: false })

export function TaskConversation({ settings, room, identity, approvalBusy, approvalErrors, onApproval }: {
  settings: Settings; room: TaskRoom; identity: Identity | null; approvalBusy: Record<string, boolean>; approvalErrors: Record<string, string>
  onApproval: (approval: Approval, decision?: ApprovalDecision, note?: string) => void
}) {
  // Each approval is shown once: under the first Agent message that proposed it, otherwise in the trailing section.
  const linked: Record<string, Approval> = {}
  const used = new Set<string>()
  for (const event of room.events) {
    if (event.kind !== 'assistant') continue
    const approval = room.approvals?.find(item => item.approval_id === event.data?.approval_id)
    if (approval && !used.has(approval.approval_id)) { linked[event.id] = approval; used.add(approval.approval_id) }
  }
  const unlinked = (room.approvals || []).filter(item => !used.has(item.approval_id))
  const actionable = room.approvals?.find(item => room.phase === 'awaiting_confirmation' ? item.status === 'draft'
    : room.phase === 'awaiting_approval' ? item.status === 'pending'
    : room.phase === 'manual_review' ? item.execution === 'manual_review' || item.lifecycle === 'manual_review'
    : room.phase === 'executing' ? ['queued', 'executing'].includes(item.execution || '')
    : ['draft', 'pending'].includes(item.status)) || room.approvals?.[0]
  const card = (approval: Approval) => (
    <div key={approval.approval_id} id={approval.approval_id === actionable?.approval_id ? 'task-panel-approvals' : undefined}>
      <ApprovalCard approval={approval} identity={identity} busy={!!approvalBusy[approval.approval_id]} error={approvalErrors[approval.approval_id] || ''}
        allowClarification={room.status === 'open'} onAction={(decision, note) => onApproval(approval, decision, note)} />
    </div>
  )

  return (
    <div className="flex flex-col gap-4" role="log" aria-label="共享事项讨论" aria-live="polite">
      {groupTaskEvents(room.events).map(group => {
        const event = group[0]!
        if (group.length > 1) return <details key={event.id} className="rounded-lg border border-dashed px-3 py-2 text-[13px]">
          <summary className="cursor-pointer text-muted-foreground">{group.length} 条事项更新 · {[...new Set(group.map(item => roles[item.kind] || '系统'))].join('、')}</summary>
          <div className="mt-2 flex flex-col gap-2">{group.map(item => <SystemEvent key={item.id} event={item} />)}</div>
        </details>
        return people.has(event.kind)
          ? <Message key={event.id} event={event} mine={event.actor === identity?.subject} settings={settings} room={room}>{linked[event.id] && card(linked[event.id]!)}</Message>
          : <SystemEvent key={event.id} event={event} />
      })}
      {unlinked.length > 0 && <section aria-label="其他事项审批" className="flex flex-col gap-2">{unlinked.map(card)}</section>}
    </div>
  )
}

function SystemEvent({ event }: { event: TaskEvent }) {
  return <div className="flex flex-wrap items-start gap-2 rounded-md border border-dashed border-border px-3 py-2 text-[13px] text-muted-foreground">
    <StatusChip>{roles[event.kind] || '系统'}</StatusChip>
    <p className="min-w-0 flex-1 pt-0.5 whitespace-pre-wrap break-words text-foreground/80">{event.text}</p>
    <time className="shrink-0 pt-0.5">{formatTime(event.created_at)}</time>
  </div>
}

function Message({ event, mine, settings, room, children }: { event: TaskEvent; mine: boolean; settings: Settings; room: TaskRoom; children?: React.ReactNode }) {
  const assistant = event.kind === 'assistant'
  const { state } = useTaskCollaboration()
  const reviewer = state.data?.reviewers?.includes(event.actor)
  return (
    <article id={`task-event-${event.id}`} tabIndex={-1} className="fade-up flex gap-3">
      {assistant ? <BotAvatar /> : <Avatar className="size-6 shrink-0 text-xs"><AvatarFallback>{(event.actor_name || '?').slice(0, 1).toUpperCase()}</AvatarFallback></Avatar>}
      <div className="min-w-0 flex-1">
        <div className="mb-1 flex flex-wrap items-center gap-2 text-[13px]">
          <strong className="text-sm">{event.actor_name}</strong>
          <StatusChip tone={assistant ? 'accent' : mine ? 'success' : 'default'}>{reviewer && !assistant ? '指定审批人' : roles[event.kind] || '系统'}</StatusChip>
          {event.kind === 'approver' && !reviewer && <span className="text-xs text-muted-foreground">发言时具备审批资格</span>}
          <time className="text-muted-foreground">{formatTime(event.created_at)}</time>
        </div>
        <div className={`rounded-lg border px-3.5 py-2.5 text-sm ${assistant ? 'border-border bg-muted/60' : 'border-border bg-card'}`}>
          {assistant ? <MarkdownMessage text={event.text} /> : <p className="prose-reply">{event.text}</p>}
          {children}
        </div>
        {assistant && <FeedbackButtons settings={settings} path={`/tasks/${room.id}/events/${event.id}/feedback`} />}
      </div>
    </article>
  )
}
