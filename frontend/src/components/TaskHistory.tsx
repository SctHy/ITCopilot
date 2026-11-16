// Find earlier tasks the viewer may access, continue them read-only, or link them to the current room.
import { useEffect, useMemo, useRef } from 'react'
import { Button } from '@/components/ui/button'
import { ExternalLinkIcon, LinkIcon, SearchIcon } from 'lucide-react'
import { useStore } from '../hooks/useStore.ts'
import { createTaskHistoryController, type TaskHistoryTransport } from '../lib/taskHistoryController.ts'
import type { TaskRoom } from '../lib/types.ts'
import { ErrorAlert, StatusChip, TextInput, type Tone } from './ui.tsx'

const statuses: Record<string, string> = { open: '跟进中', stopped: '已停止', resolved: '已解决' }
const statusTones: Record<string, Tone> = { open: 'accent', stopped: 'default', resolved: 'success' }
const approvalStatuses: Record<string, string> = { draft: '待确认', pending: '待审批', approved: '已批准', rejected: '已拒绝', expired: '已到期', superseded: '已替代', invalidated: '已失效', withdrawn: '已撤回' }
const executions: Record<string, string> = { not_executed: '未执行', queued: '排队中', executing: '执行中', succeeded: '成功', failed: '失败', manual_review: '需人工复核' }
const followup = '请根据当前审批、执行和任务进度继续只读跟进；需要新变更时请说明申请步骤。'

/** Renders the history search; runs the first search once `active` turns true. */
export function TaskHistory({ client, room = null, canManage = false, active, onOpen }: {
  client: TaskHistoryTransport; room?: TaskRoom | null; canManage?: boolean; active: boolean; onOpen: (id: string) => void
}) {
  const onOpenRef = useRef(onOpen)
  onOpenRef.current = onOpen
  const controller = useMemo(() => createTaskHistoryController(client, room?.id, id => onOpenRef.current(id)), [client, room?.id])
  const state = useStore(controller.state)
  const loaded = useRef(false)
  useEffect(() => () => controller.dispose(), [controller])
  useEffect(() => { if (active && !loaded.current) { loaded.current = true; controller.search() } }, [active, controller])

  return (
    <div className="flex flex-col gap-3">
      <p className="text-[13px] text-muted-foreground">只展示你当前有权访问的事项。继续处理会回到原事项，并重新读取当前状态。</p>
      <form className="flex items-end gap-2" onSubmit={event => { event.preventDefault(); loaded.current = true; controller.search() }}>
        <TextInput label="关键词" value={state.query} onChange={value => { state.query = value }} maxLength={200} placeholder="例如 GitLab、VPN 或事项名称" />
        <Button type="submit" variant="secondary" className="shrink-0" aria-busy={state.busy} disabled={state.busy}><SearchIcon className="size-4" />{state.busy ? '查询中' : '搜索'}</Button>
      </form>
      {state.error && <ErrorAlert>{state.error}</ErrorAlert>}
      {room && state.links.length > 0 && (
        <div className="rounded-md bg-muted p-3">
          <p className="mb-2 text-[13px] font-semibold text-muted-foreground">当前可见的关联事项</p>
          <ul className="grid gap-1.5">
            {state.links.map(item => (
              <li key={item.id} className="flex flex-wrap items-center gap-2 text-sm">
                <Button type="button" size="sm" variant="ghost" onClick={() => onOpen(item.id)}><LinkIcon className="size-3.5" />{item.title}</Button>
                <StatusChip tone={statusTones[item.status] || 'default'}>{statuses[item.status] || item.status}</StatusChip>
                {canManage && <Button type="button" size="sm" variant="ghost" className="text-destructive-foreground" onClick={() => controller.link(item.id, false)}>取消关联</Button>}
              </li>
            ))}
          </ul>
        </div>
      )}
      <div className="grid gap-2">
        {state.items.map(item => (
          <article key={item.id} className="rounded-md border border-border p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <strong className="text-sm">{item.title}</strong>
              <StatusChip tone={statusTones[item.status] || 'default'}>{statuses[item.status] || item.status}</StatusChip>
            </div>
            <p className="mt-1 text-[13px] text-muted-foreground">步骤 {item.steps_done}/{item.steps_total} · 未完待办 {item.pending_todos} · Agent 回合 {item.active_runs}</p>
            {item.goal && <p className="mt-2 text-sm">{item.goal}</p>}
            {item.blocker && <p className="mt-1 text-[13px] text-warning-foreground">阻塞：{item.blocker}</p>}
            {item.next_action && <p className="mt-1 text-[13px] text-muted-foreground">下一步：{item.next_action}</p>}
            {item.resolution && <p className="mt-1 text-[13px] text-success-foreground">历史验收：{item.resolution}</p>}
            {(item.approvals || []).map(approval => (
              <p key={approval.approval_id} className="mt-1 text-[13px] text-muted-foreground">审批 {approvalStatuses[approval.status] || approval.status} · 执行 {executions[approval.execution] || approval.execution}</p>
            ))}
            <div className="mt-3 flex flex-wrap gap-2">
              <Button type="button" size="sm" variant="secondary" onClick={() => onOpen(item.id)}>打开原事项<ExternalLinkIcon className="size-3" /></Button>
              {item.can_continue && (
                <Button type="button" size="sm" variant="outline" disabled={(!!state.continuing) || (state.continuing === item.id)} aria-busy={state.continuing === item.id} onClick={() => controller.resume(item, followup)}>
                  {item.status === 'open' ? '继续只读跟进' : '重新打开并只读跟进'}
                </Button>
              )}
              {room && canManage && !state.links.some(link => link.id === item.id) && (
                <Button type="button" size="sm" variant="ghost" onClick={() => controller.link(item.id, true)}><LinkIcon className="size-3.5" />关联到当前事项</Button>
              )}
            </div>
          </article>
        ))}
      </div>
      {loaded.current && !state.busy && !state.items.length && <p className="text-[13px] text-muted-foreground">没有匹配且可访问的历史事项。</p>}
      {room && <p className="text-xs text-muted-foreground">关联不会复制讨论内容，也不会授予源事项访问权。</p>}
    </div>
  )
}
