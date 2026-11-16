import { useEffect } from 'react'
import { Accordion } from '@/components/ui/accordion'
import { Spinner } from '@/components/ui/spinner'
import type { Approval } from '../../lib/types.ts'
import { useTaskCollaboration } from './TaskCollaboration.tsx'
import { TaskHistory } from '../TaskHistory.tsx'
import { ErrorAlert } from '../ui.tsx'
import { FilesPanel, MembersPanel, NotificationsPanel, PanelItem, ProgressPanel, ReminderPanel, RunsPanel, TodoPanel, VerificationPanel } from './TaskPanels.tsx'
import { TaskBriefPanel } from './TaskBriefPanel.tsx'

/** Workspace of one room; the parent keys it by room id so each room gets its own controller. */
export function TaskDetails({ clarify, onOpen }: {
  clarify: { approval: Approval; at: number } | null; onOpen: (id: string) => void
}) {
  const { state, room, identity, client, expanded, setExpanded, canManage, isAdmin, act, changed } = useTaskCollaboration()
  useEffect(() => { if (clarify) setExpanded(current => current.includes('todos') ? current : [...current, 'todos']) }, [clarify])
  const data = state.data

  return (
    <section aria-label="任务协作与运行" className="flex flex-col gap-3">
      {state.error && <ErrorAlert>{state.error}</ErrorAlert>}
      {!data ? (
        <div className="flex items-center justify-center gap-2 py-10 text-sm text-muted-foreground"><Spinner className="size-4" />正在读取事项工作区…</div>
      ) : (
        <Accordion multiple value={expanded} onValueChange={value => setExpanded(value as string[])} className="rounded-xl border bg-card">
          <TaskBriefPanel brief={data.brief} room={room} />
          <ProgressPanel progress={data.progress} suggestions={data.suggestions || []} room={room} canManage={canManage} busy={state.busy} act={act} />
          {data.verification ? (
            <VerificationPanel verification={data.verification} checkpoints={data.checkpoints || []} room={room} canManage={canManage} busy={state.busy} act={act} />
          ) : null}
          <TodoPanel members={data.members || []} reviewers={data.reviewers || []} todos={data.todos} room={room} identity={identity} busy={state.busy} act={act} clarify={clarify} />
          <ReminderPanel reminders={data.reminders} room={room} canManage={canManage} busy={state.busy} act={act} />
          <FilesPanel files={data.files || []} room={room} client={client} onChanged={changed} />
          <MembersPanel members={data.members || []} reviewers={data.reviewers || []} room={room} client={client} canManage={canManage} isAdmin={isAdmin} busy={state.busy} act={act} />
          {data.notifications ? (
            <NotificationsPanel notifications={data.notifications} members={data.members || []} canManage={canManage} busy={state.busy} act={act} />
          ) : null}
          <RunsPanel budget={data.budget} attempts={data.attempts} isAdmin={isAdmin} busy={state.busy} act={act} />
          <PanelItem id="history" title="历史事项与关联流程">
            <TaskHistory client={client} room={room} canManage={canManage} active={expanded.includes('history')} onOpen={onOpen} />
          </PanelItem>
        </Accordion>
      )}
    </section>
  )
}
