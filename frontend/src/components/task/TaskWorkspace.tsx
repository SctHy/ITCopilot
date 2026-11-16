// Shared tasks: requester, Agent and approvers work in one room. App remounts this when identity or endpoint changes.
import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from '@tanstack/react-router'
import { Button } from '@/components/ui/button'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Separator } from '@/components/ui/separator'
import { Spinner } from '@/components/ui/spinner'
import { ArchiveIcon, ChevronsUpIcon, EyeIcon, LinkIcon, MessagesSquareIcon, PlusIcon, RefreshCwIcon, SendIcon, SquareIcon } from 'lucide-react'
import { usePolling, useStore } from '../../hooks/useStore.ts'
import { createTaskClient } from '../../lib/taskClient.ts'
import { createTaskController } from '../../lib/taskController.ts'
import type { Approval, ApprovalDecision, Identity, Settings } from '../../lib/types.ts'
import { CheckInput, Empty, ErrorAlert, Hint, InfoAlert, StatusChip, TextAreaInput, TextInput, errorText, type Tone } from '../ui.tsx'
import { SpeechInput } from '../SpeechInput.tsx'
import { TaskConversation } from './TaskConversation.tsx'
import { TaskDetails } from './TaskDetails.tsx'
import { TaskCollaborationProvider, TaskNextActions, TaskParticipantsButton } from './TaskCollaboration.tsx'

const phases: Record<string, string> = { working: 'Agent 处理中', waiting: '等待后续消息', awaiting_confirmation: '等待申请人确认提交', awaiting_approval: '等待审批', executing: '业务执行中', manual_review: '需人工核对', failed: 'Agent 回合失败', stopped: '已停止跟进', resolved: '已解决', budget_paused: '累计预算已用完，等待管理员调整' }
const phaseTones: Record<string, Tone> = { working: 'accent', awaiting_confirmation: 'accent', awaiting_approval: 'warning', executing: 'accent', manual_review: 'warning', failed: 'danger', resolved: 'success', budget_paused: 'warning' }
const statusLabels: Record<string, string> = { open: '跟进中', resolved: '已解决', stopped: '已停止' }
const statusTones: Record<string, Tone> = { open: 'accent', resolved: 'success', stopped: 'default' }

export function TaskWorkspace({ settings, identity, requestedTask = null, onChanged }: {
  settings: Settings; identity: Identity | null; requestedTask?: { id: string; at: number } | null; onChanged?: () => void
}) {
  const navigate = useNavigate()
  const mounted = useRef(true)
  useEffect(() => () => { mounted.current = false }, [])
  const selectTask = (id: string) => navigate({ to: '/tasks', search: { task: id } })
  const beginNew = () => navigate({ to: '/tasks', search: { new: true } })
  const client = useMemo(() => createTaskClient(settings), [])  // eslint-disable-line react-hooks/exhaustive-deps
  // Open the room named in ?task= before the first read; the effect below keeps the URL in step with the open room.
  const controller = useMemo(() => {
    const created = createTaskController(client)
    created.state.selected = requestedTask?.id || ''
    created.state.creating = requestedTask?.id === ''
    return created
  }, [client])
  const state = useStore(controller.state)
  const [speechBusy, setSpeechBusy] = useState(false)
  const [clarify, setClarify] = useState<{ approval: Approval; at: number } | null>(null)
  usePolling(controller.refresh, 2000)
  useEffect(() => () => controller.dispose(), [controller])
  useEffect(() => { onChanged?.() }, [state.selected]) // Refresh sidebar after a new room is created.

  async function createTask() {
    await controller.create()
    if (mounted.current && controller.state.selected && !controller.state.creating) {
      await navigate({ to: '/tasks', search: { task: controller.state.selected }, replace: true })
    }
  }

  const room = state.room
  const query = state.query
  const failedRuns = (room?.runs || []).filter(run => run.status === 'failed')
  const canStop = room?.requester === identity?.subject || !!identity?.roles?.includes('it_admin')

  async function handleApproval(approval: Approval, decision?: ApprovalDecision, note?: string) {
    if (decision === 'clarify') { setClarify({ approval, at: Date.now() }); return }
    await controller.decide(approval, decision, note)
  }
  async function archive() {
    try { await client.capability(state.selected, 'archive', { archived: !query.archived }); await controller.refresh() } catch (e) { state.error = errorText(e) }
  }
  async function markRead() {
    try { await client.capability(state.selected, 'read', { seq: Math.max(0, ...(room?.events ?? []).map(e => e.seq)) }); await controller.refresh() }
    catch (e) { state.error = errorText(e) }
  }
  async function copyLink() {
    const url = new URL(window.location.href)
    url.pathname = '/tasks'; url.search = ''; url.hash = ''; url.searchParams.set('task', state.selected)
    try { await navigator.clipboard.writeText(url.href); state.notice = '事项链接已复制；访问者仍需登录并具备相应权限。' }
    catch { state.notice = url.href }
  }

  return (
    <section className="flex h-full min-h-0 flex-col">

      {/* Room */}
      <main className="min-h-0 min-w-0 flex-1 overflow-y-auto">
        <div className="mx-auto flex w-full max-w-6xl flex-col gap-4 p-4 sm:p-6">
          {state.error && <ErrorAlert>{state.error}</ErrorAlert>}
          {state.notice && <InfoAlert status="success">{state.notice}</InfoAlert>}

          {state.creating ? (
            <Card className="mx-auto w-full max-w-2xl gap-0 py-0">
              <CardHeader className="border-b px-6 py-5">
                <CardTitle className="text-lg">发起共享事项</CardTitle>
                <CardDescription>创建后可点击「添加参与人」邀请同事。这里的记录与私人对话分开保存。</CardDescription>
              </CardHeader>
              <form className="flex flex-col gap-4 px-6 py-5" onSubmit={event => { event.preventDefault(); if (!speechBusy) createTask() }}>
                <TextInput label="事项名称" value={state.title} onChange={value => { state.title = value }} isRequired maxLength={200} placeholder="例如：仓库权限申请与访问排查" />
                <SpeechInput key={JSON.stringify(['new', settings.backend, settings.endpoints, settings.accessToken])}
                  settings={settings} disabled={state.busy} onBusy={setSpeechBusy}
                  value={state.initialMessage} onChange={text => { state.initialMessage = text }} />
                <TextAreaInput label="需求描述" value={state.initialMessage} onChange={value => { state.initialMessage = value }} isRequired maxLength={8000} rows={5}
                  placeholder="描述需要办理的事情，以及完成后还需要 Agent 跟进什么。" />
                <div>
                  <Button type="submit" aria-busy={state.busy} disabled={state.busy || speechBusy || !state.title.trim() || !state.initialMessage.trim()}>{state.busy ? '提交中' : '创建并请 Agent 处理'}</Button>
                </div>
              </form>
            </Card>
          ) : room ? (
            <TaskCollaborationProvider key={room.id} room={room} identity={identity} client={client} onChanged={controller.refresh}>
              <Card className="gap-3 p-5">
                <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                  <div className="min-w-0">
                    <h2 className="text-xl font-semibold tracking-tight">{room.title}</h2>
                    <div className="mt-2 flex flex-wrap items-center gap-2 text-[13px] text-muted-foreground">
                      申请人 {room.requester_name}
                      <StatusChip tone={phaseTones[room.phase] || 'default'}>{phases[room.phase] || room.phase}</StatusChip>
                      <StatusChip tone={statusTones[room.status] || 'default'}>{statusLabels[room.status] || room.status}</StatusChip>
                    </div>
                  </div>
                  <div className="flex shrink-0 flex-wrap gap-1">
                    <TaskParticipantsButton />
                    <Hint label={<>复制事项链接</>}><Button type="button" size="icon" variant="ghost" aria-label="复制事项链接" onClick={copyLink}><LinkIcon className="size-4" /></Button></Hint>
                    <Hint label={<>标记当前消息已读</>}><Button type="button" size="icon" variant="ghost" aria-label="标记当前消息已读" onClick={markRead}><EyeIcon className="size-4" /></Button></Hint>
                    <Hint label={<>{query.archived ? '移出归档' : '归档到个人列表'}</>}><Button type="button" size="icon" variant="ghost" aria-label={query.archived ? '移出归档' : '归档到个人列表'} onClick={archive}><ArchiveIcon className="size-4" /></Button></Hint>
                  </div>
                </div>
                <p className="rounded-md bg-muted px-3 py-2 text-[13px] text-muted-foreground">在讨论中说“同意”不会执行操作。请在对应消息的审批卡片中选择「同意审批」或「拒绝申请」。</p>
              </Card>
              <TaskNextActions />

              <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1fr)_400px]">
                <div className="flex min-w-0 flex-col gap-4">
                  {room.has_more_events && <div className="text-center"><Button type="button" size="sm" variant="ghost" onClick={() => controller.older()}><ChevronsUpIcon className="size-3.5" />加载更早的讨论</Button></div>}
                  <TaskConversation settings={settings} room={room} identity={identity} approvalBusy={state.approvalBusy} approvalErrors={state.approvalErrors} onApproval={handleApproval} />
                  {room.phase === 'working' && (
                    <p role="status" className="flex items-center gap-2 text-[13px] text-muted-foreground"><Spinner className="size-4" />Agent 正在处理，可以继续讨论或离开页面，结果会保留在这里。</p>
                  )}
                  {failedRuns.map(run => (
                    <div key={run.id} className="flex items-center justify-between gap-3 rounded-md border border-border bg-card px-4 py-3 text-sm text-destructive-foreground">
                      <span>有一个 Agent 回合处理失败。</span>
                      {room.can_write !== false && <Button type="button" size="sm" variant="destructive" disabled={state.busy || room.status !== 'open'} onClick={() => controller.retry(run.id)}><RefreshCwIcon className="size-3.5" />重新处理</Button>}
                    </div>
                  ))}
                  {room.status === 'open' && room.can_write !== false && (
                    <Card className="gap-3 p-3">
                      <form onSubmit={event => { event.preventDefault(); if (!speechBusy) controller.send() }} className="flex flex-col gap-2">
                        <SpeechInput key={JSON.stringify([room.id, settings.backend, settings.endpoints, settings.accessToken])}
                          settings={settings} disabled={state.busy} onBusy={setSpeechBusy}
                          value={state.message} onChange={text => { if (room.status === 'open' && room.can_write !== false) state.message = text }} />
                        <TextAreaInput ariaLabel="参与讨论" value={state.message} onChange={value => { state.message = value }} isRequired maxLength={8000} rows={3}
                          placeholder="补充需求、询问依据，或请 Agent 继续排查…" />
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <CheckInput isSelected={state.askAgent} onChange={value => { state.askAgent = value }}>同时请 Agent 回复</CheckInput>
                          <div className="flex gap-2">
                            {canStop && (
                              <Hint label={<>停止跟进仅停止 Agent 回合，现有审批与已批准的业务动作继续按原工作流处理。</>}><Button type="button" size="sm" variant="outline" disabled={state.busy} onClick={() => controller.stop()}><SquareIcon className="size-3.5" />停止跟进</Button></Hint>
                            )}
                            <Button size="sm" type="submit" aria-busy={state.busy} disabled={state.busy || speechBusy || !state.message.trim()}><SendIcon className="size-3.5" />发送</Button>
                          </div>
                        </div>
                      </form>
                    </Card>
                  )}
                </div>
                <div className="xl:sticky xl:top-4">
                  <Separator className="mb-4 xl:hidden" />
                  <TaskDetails clarify={clarify} onOpen={selectTask} />
                </div>
              </div>
            </TaskCollaborationProvider>
          ) : state.selected && !state.error ? (
            <div className="flex items-center justify-center gap-2 py-20 text-sm text-muted-foreground"><Spinner className="size-4" />正在打开事项…</div>
          ) : (
            <div className="py-16">
              <Empty icon={<MessagesSquareIcon />} title="在左侧选择一个事项，查看共同讨论与处理结果" description="申请人、Agent 与审批人在同一事项中协作。邀请成员或指定审批人后，对方才能访问。可在左侧搜索、筛选或新建事项。">
                <Button type="button" className="mt-3" onClick={beginNew}><PlusIcon className="size-4" />新建事项</Button>
              </Empty>
            </div>
          )}
        </div>
      </main>
    </section>
  )
}
