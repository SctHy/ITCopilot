import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogClose, DialogDescription, DialogFooter, DialogHeader, DialogContent, DialogTitle, DialogTrigger } from '@/components/ui/dialog'
import { usePolling, useStore } from '../../hooks/useStore.ts'
import { createTaskDetailsController } from '../../lib/taskDetailsController.ts'
import { taskNextAction } from '../../lib/taskPresentation.ts'
import type { TaskClient } from '../../lib/taskClient.ts'
import type { Identity, TaskRoom } from '../../lib/types.ts'
import { ErrorAlert, StatusChip } from '../ui.tsx'
import { MembersEditor } from './TaskPanels.tsx'

function useCollaborationState(room: TaskRoom, identity: Identity | null, client: TaskClient, onChanged: () => void) {
  const controller = useMemo(() => createTaskDetailsController(client, room.id), [client, room.id])
  const state = useStore(controller.state)
  const [expanded, setExpanded] = useState<string[]>(['brief', 'progress', 'todos'])
  useEffect(() => () => controller.dispose(), [controller])
  usePolling(controller.refresh, 2000)
  const act = async (action: string, payload?: Record<string, unknown>) => {
    const ok = await controller.perform(action, payload)
    if (ok) onChanged()
    return ok
  }
  const changed = async () => { await controller.refresh(); onChanged() }
  const openPanel = (panel: string) => {
    const section = panel === 'suggestions' ? 'progress' : panel
    setExpanded(current => current.includes(section) ? current : [...current, section])
    requestAnimationFrame(() => {
      const element = document.getElementById(`task-panel-${panel}`)
      element?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
      element?.querySelector<HTMLElement>('button')?.focus({ preventScroll: true })
    })
  }
  return { state, room, identity, client, act, changed, expanded, setExpanded, openPanel,
    canManage: room.requester === identity?.subject || !!identity?.roles?.includes('it_admin'),
    isAdmin: !!identity?.roles?.includes('it_admin') }
}

const Collaboration = createContext<ReturnType<typeof useCollaborationState> | null>(null)
export function TaskCollaborationProvider({ room, identity, client, onChanged, children }: {
  room: TaskRoom; identity: Identity | null; client: TaskClient; onChanged: () => void; children: ReactNode
}) {
  const value = useCollaborationState(room, identity, client, onChanged)
  return <Collaboration.Provider value={value}>{children}</Collaboration.Provider>
}
export function useTaskCollaboration() {
  const value = useContext(Collaboration)
  if (!value) throw new Error('Task collaboration provider is required')
  return value
}

export function TaskNextActions() {
  const { state, room, identity, openPanel } = useTaskCollaboration()
  if (!state.data) return null
  const next = taskNextAction(room, state.data, identity?.subject)
  return (
    <section aria-label="当前待办与下一步" className="rounded-xl border bg-card p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <h3 className="text-sm font-semibold">{next.title}</h3>
          <p className="mt-1 whitespace-pre-wrap break-words text-sm text-muted-foreground">{next.text}</p>
          {state.error && <p className="mt-1 text-xs text-warning-foreground">刷新失败，以下为上次读取的状态。</p>}
        </div>
        {next.panel && <Button type="button" size="sm" variant="outline" onClick={() => openPanel(next.panel!)}>{next.label}</Button>}
      </div>
      {next.todos.length > 0 && <ul className="mt-3 grid gap-2 border-t pt-3">
        {next.todos.slice(0, 3).map(todo => <li key={todo.id} className="flex flex-wrap items-start gap-2 text-sm">
          <StatusChip tone={todo.mine ? 'accent' : 'default'}>{todo.mine ? '待你补充' : todo.name}</StatusChip>
          <span className="min-w-0 flex-1 break-words">{todo.question}</span>
          {todo.due_at && <time dateTime={new Date(todo.due_at * 1000).toISOString()} className="text-xs text-muted-foreground">
            {todo.due_at * 1000 < Date.now() ? '已逾期 · ' : '截止 '}{new Date(todo.due_at * 1000).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })}
          </time>}
        </li>)}
        <li><Button type="button" size="sm" variant="ghost" onClick={() => openPanel('todos')}>查看全部 {next.todos.length} 项待办</Button></li>
      </ul>}
    </section>
  )
}

export function TaskParticipantsButton() {
  const { state, room, client, canManage, isAdmin, act } = useTaskCollaboration()
  const [open, setOpen] = useState(false)
  if (!canManage) return null
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger render={<Button type="button" size="sm" variant="outline" />}>添加参与人</DialogTrigger>
      <DialogContent className="max-h-[90svh] overflow-y-auto">
        <DialogHeader><DialogTitle>参与人与访问权限</DialogTitle><DialogDescription>添加同事共同讨论，或授予只读查看权限。</DialogDescription></DialogHeader>
        <div>
          {state.error && <ErrorAlert>{state.error}</ErrorAlert>}
          {open && state.data ? <MembersEditor members={state.data.members || []} reviewers={state.data.reviewers || []} room={room} client={client}
            canManage={canManage} isAdmin={isAdmin} busy={state.busy} act={act} /> : <p className="text-sm text-muted-foreground">正在读取成员信息…</p>}
        </div>
        <DialogFooter><DialogClose render={<Button type="button" variant="outline" />}>完成</DialogClose></DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
