// Collaboration panels of a shared task. Each panel keeps its own drafts so the 2s workspace poll never wipes them.
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { AccordionItem, AccordionContent, AccordionTrigger } from '@/components/ui/accordion'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { DownloadIcon, PlusIcon, Trash2Icon } from 'lucide-react'
import type { TaskClient } from '../../lib/taskClient.ts'
import type { TaskAct } from '../../lib/taskDetailsController.ts'
import { progressChanges } from '../../lib/taskPresentation.ts'
import type {
  Approval, DirectoryPerson, GoalCheck, Identity, TaskAttempt, TaskBudget, TaskCheckpoint, TaskFile, TaskMember, TaskNotifications,
  TaskProgress, TaskReminder, TaskRoom, TaskSuggestion, TaskTodo, TaskVerification
} from '../../lib/types.ts'
import { CheckInput, ErrorAlert, Gauge, InfoAlert, MultiSelectInput, SelectInput, StatusChip, TextAreaInput, TextInput, errorText, type Tone } from '../ui.tsx'

type Busy = Record<string, boolean>

export function PanelItem({ id, title, badge, children }: { id: string; title: string; badge?: ReactNode; children: ReactNode }) {
  return (
    <AccordionItem id={`task-panel-${id}`} value={id} className="border-b px-4 last:border-b-0">
      <AccordionTrigger className="py-3.5 hover:no-underline">
        <span className="flex flex-1 items-center gap-2">{title}{badge}</span>
      </AccordionTrigger>
      <AccordionContent>
        <div className="flex flex-col gap-3 pb-1 text-sm">{children}</div>
      </AccordionContent>
    </AccordionItem>
  )
}

const Count = ({ n, tone = 'default' }: { n: number; tone?: Tone }) => n ? <StatusChip tone={tone}>{n}</StatusChip> : null
const Muted = ({ children }: { children: ReactNode }) => <p className="text-[13px] leading-relaxed text-muted-foreground">{children}</p>
const Row = ({ children }: { children: ReactNode }) => <div className="flex flex-wrap items-center gap-2">{children}</div>
const Item = ({ children }: { children: ReactNode }) => <article className="rounded-md border border-border p-3">{children}</article>
const formatSeconds = (seconds: number) => new Date(seconds * 1000).toLocaleString('zh-CN', { hour12: false })
const stageLabels: Record<string, string> = { todo: '待处理', working: '进行中', done: '已完成' }
const stageTones: Record<string, Tone> = { todo: 'default', working: 'accent', done: 'success' }
const stageOptions = [{ id: 'todo', label: '待处理' }, { id: 'working', label: '进行中' }, { id: 'done', label: '已完成' }]

function Steps({ steps, doneLabel = '已完成' }: { steps: TaskProgress['steps']; doneLabel?: string }) {
  if (!steps.length) return null
  return (
    <ol className="grid gap-1.5">
      {steps.map((step, index) => (
        <li key={index} className="flex items-start gap-2">
          <StatusChip tone={stageTones[step.status] || 'default'}>{step.status === 'done' ? doneLabel : stageLabels[step.status] || step.status}</StatusChip>
          <span className="pt-0.5">{step.title}<small className="ml-1 text-[13px] text-muted-foreground">（{step.evidence_ids.length} 条证据）</small></span>
        </li>
      ))}
    </ol>
  )
}

// ---- Agent progress suggestions ----
const suggestionLabels: Record<string, string> = { pending: '待处理', accepted: '已采纳', dismissed: '已忽略', superseded: '已被后续建议替代' }
function ProgressSuggestions({ suggestions, progress, room, busy, act }: { suggestions: TaskSuggestion[]; progress: TaskProgress; room: TaskRoom; busy: Busy; act: TaskAct }) {
  const pending = suggestions.filter(item => item.status === 'pending')
  const history = suggestions.filter(item => item.status !== 'pending')
  return (
    <section id="task-panel-suggestions" aria-label="建议的进度变化" className="space-y-3">
      {pending.map(item => {
        const stale = item.base_version !== progress.version
        const changes = progressChanges(progress, item.payload)
        return <Item key={item.id}>
          <Row><strong>建议更新</strong><StatusChip tone={stale ? 'default' : 'accent'}>{stale ? '基于旧进度' : `${changes.length} 项变化`}</StatusChip></Row>
          {stale ? <Muted>当前进度已变化，此建议不能直接覆盖。可查看原建议后手动编辑。</Muted> : <>
            {!changes.length && <Muted>与当前进度一致，无需重复采纳。</Muted>}
            <ul className="mt-2 space-y-2">
              {changes.map((change, index) => <li key={index} className="text-[13px]">
                <strong className="text-foreground">{change.label}</strong>
                {change.before && <p className="mt-1 whitespace-pre-wrap break-words text-muted-foreground">原：{change.before}</p>}
                <p className="whitespace-pre-wrap break-words text-foreground">新：{change.after || '清除'}</p>
              </li>)}
            </ul>
          </>}
          <details className="mt-2 text-[13px]"><summary className="cursor-pointer text-muted-foreground">查看完整建议</summary>
            <p className="mt-2">{item.payload.goal}</p><Steps steps={item.payload.steps} doneLabel="有完成证据" />
            {item.payload.blocker && <p>阻塞：{item.payload.blocker}</p>}<p>下一步：{item.payload.next_action || '未填写'}</p>
          </details>
          {room.can_write !== false && room.status === 'open' && (
            <div className="mt-3 flex flex-col gap-2">
              <Row>
                <Button type="button" size="sm" disabled={busy['suggestions/' + item.id] || stale || !changes.length} onClick={() => act('suggestions/' + item.id, { accept: true })}>确认更新进度</Button>
                <Button type="button" size="sm" variant="ghost" disabled={busy['suggestions/' + item.id]} onClick={() => act('suggestions/' + item.id, { accept: false })}>忽略</Button>
              </Row>
            </div>
          )}
        </Item>
      })}
      {!!history.length && <details className="text-[13px] text-muted-foreground">
        <summary className="cursor-pointer">最近已处理的建议（{history.length}）</summary>
        <ul className="mt-2 space-y-2">{history.map(item => <li key={item.id}><StatusChip>{suggestionLabels[item.status] || item.status}</StatusChip><span className="ml-2">{item.payload.goal}</span></li>)}</ul>
      </details>}
    </section>
  )
}

// ---- Progress and acceptance ----
export function ProgressPanel({ progress, suggestions, room, canManage, busy, act }: { progress: TaskProgress; suggestions: TaskSuggestion[]; room: TaskRoom; canManage: boolean; busy: Busy; act: TaskAct }) {
  const [draft, setDraft] = useState<TaskProgress | null>(null)
  const [resolution, setResolution] = useState('')
  const events = room.events.map(event => ({ id: event.id, label: `${event.actor_name}：${event.text.slice(0, 70)}` }))
  const update = (patch: Partial<TaskProgress>) => setDraft(current => current && { ...current, ...patch })
  const updateStep = (index: number, patch: Partial<TaskProgress['steps'][number]>) =>
    setDraft(current => current && { ...current, steps: current.steps.map((step, i) => i === index ? { ...step, ...patch } : step) })
  const done = progress.steps.filter(step => step.status === 'done').length
  async function save() { if (draft && await act('progress', { ...draft })) setDraft(null) }
  return (
    <PanelItem id="progress" title="任务进度与验收" badge={progress.steps.length ? <StatusChip tone={done === progress.steps.length ? 'success' : 'default'}>{done}/{progress.steps.length}</StatusChip> : null}>
      <ProgressSuggestions suggestions={suggestions} progress={progress} room={room} busy={busy} act={act} />
      {progress.goal && <p className="font-medium">{progress.goal}</p>}
      {progress.steps.length > 0 && (
        <Gauge value={(done / progress.steps.length) * 100} />
      )}
      {progress.blocker && <p className="text-[13px] text-warning-foreground">阻塞：{progress.blocker}</p>}
      {progress.next_action && <p className="text-[13px] text-muted-foreground">下一步：{progress.next_action}</p>}
      <Steps steps={progress.steps} />
      {progress.resolution && <InfoAlert status="success" title="验收说明">{progress.resolution}</InfoAlert>}
      {room.status === 'open' && room.can_write !== false && !draft && (
        <div><Button type="button" size="sm" variant="secondary" onClick={() => setDraft(JSON.parse(JSON.stringify(progress)))}>编辑进度</Button></div>
      )}
      {draft && (
        <form className="flex flex-col gap-3 rounded-md bg-muted p-3" onSubmit={event => { event.preventDefault(); save() }}>
          <TextInput label="目标" value={draft.goal} onChange={goal => update({ goal })} isRequired maxLength={8000} />
          {draft.steps.map((step, index) => (
            <div key={index} className="flex flex-col gap-2 rounded-md border border-border bg-card p-3">
              <TextInput ariaLabel="步骤与完成标准" value={step.title} onChange={title => updateStep(index, { title })} isRequired maxLength={200} placeholder="步骤与完成标准" />
              <SelectInput ariaLabel="步骤状态" value={step.status} onChange={status => updateStep(index, { status })} options={stageOptions} />
              <MultiSelectInput label="证据消息（可多选）" values={step.evidence_ids} onChange={evidence_ids => updateStep(index, { evidence_ids })} options={events} />
              <div><Button type="button" size="sm" variant="ghost" className="text-destructive-foreground" onClick={() => update({ steps: draft.steps.filter((_, i) => i !== index) })}><Trash2Icon className="size-3.5" />删除步骤</Button></div>
            </div>
          ))}
          <div><Button type="button" size="sm" variant="outline" disabled={draft.steps.length >= 30} onClick={() => update({ steps: [...draft.steps, { title: '', status: 'todo', evidence_ids: [] }] })}><PlusIcon className="size-3.5" />添加步骤</Button></div>
          <TextInput label="阻塞原因" value={draft.blocker || ''} onChange={blocker => update({ blocker })} maxLength={4000} />
          <TextInput label="下一步" value={draft.next_action || ''} onChange={next_action => update({ next_action })} maxLength={4000} />
          <Row><Button size="sm" type="submit" disabled={busy.progress}>保存进度</Button><Button type="button" size="sm" variant="ghost" onClick={() => setDraft(null)}>取消</Button></Row>
        </form>
      )}
      {canManage && room.status === 'open' && (
        <form className="flex items-end gap-2" onSubmit={event => { event.preventDefault(); act('lifecycle', { status: 'resolved', resolution }) }}>
          <TextInput label="验收说明" value={resolution} onChange={setResolution} isRequired maxLength={4000} placeholder="说明如何验证目标已完成" />
          <Button size="sm" type="submit" className="shrink-0" disabled={busy.lifecycle}>验收解决</Button>
        </form>
      )}
      {canManage && room.status !== 'open' && (
        <div><Button type="button" size="sm" variant="secondary" disabled={busy.lifecycle} onClick={() => act('lifecycle', { status: 'open' })}>重新打开</Button></div>
      )}
    </PanelItem>
  )
}

// ---- Independent goal verification ----
const verifyLabels: Record<string, string> = { queued: '等待核验', running: '核验中', passed: '已通过', failed: '未达标', unknown: '需人工核查', stale: '已过期，请重新核验', cancelled: '已取消' }
const verifyTones: Record<string, Tone> = { queued: 'accent', running: 'accent', passed: 'success', failed: 'danger', unknown: 'warning', stale: 'warning' }
function describe(check: GoalCheck) {
  if ('system' in check) return `${check.system} 权限至少达到 ${check.role}`
  if ('device_id' in check) return `设备 ${check.device_id} 已锁定`
  return `工单 ${check.ticket_id} 存在且未关闭`
}
export function VerificationPanel({ verification, checkpoints, room, canManage, busy, act }: {
  verification: TaskVerification; checkpoints: TaskCheckpoint[]; room: TaskRoom; canManage: boolean; busy: Busy; act: TaskAct
}) {
  const [draft, setDraft] = useState<TaskVerification['goal'] | null>(null)
  const latest = verification.history.find(item => item.id === verification.goal.latest_id)
  const roles = (system: string) => verification.catalog.find(item => item.system === system)?.roles || []
  const setCheck = (index: number, check: GoalCheck) => setDraft(current => current && { ...current, checks: current.checks.map((c, i) => i === index ? check : c) })
  const addCheck = (check: GoalCheck) => setDraft(current => current && { ...current, checks: [...current.checks, check] })
  async function save() {
    if (draft && await act('goal', { version: draft.version, checks: draft.checks, replan_limit: draft.replan_limit })) setDraft(null)
  }
  const checks = verification.goal.checks
  return (
    <PanelItem id="verification" title="目标独立核验" badge={latest ? <StatusChip tone={verifyTones[latest.status] || 'default'}>{verifyLabels[latest.status] || latest.status}</StatusChip> : null}>
      <Muted>只读核验申请人的系统权限、设备锁定与工单状态。核验通过后，由申请人验收事项；登录和实际访问仍需另行确认。</Muted>
      {checks.length ? (
        <>
          <ul className="grid gap-1">{checks.map((check, index) => <li key={index} className="flex items-center gap-2"><span className="size-1.5 rounded-full bg-accent" />{describe(check)}</li>)}</ul>
          <Muted>自动只读跟进 {verification.goal.replans_used} / {verification.goal.replan_limit} 轮 · {verification.acceptable ? '核验依据有效，等待人工验收' : '等待当前进度的有效核验结果'}；核验结果有效期 {verification.fresh_seconds / 60} 分钟，修改进度后请重新核验。</Muted>
        </>
      ) : <Muted>尚未配置自动核验条件，使用人工验收。</Muted>}
      {latest && (
        <div className="rounded-md bg-muted p-3">
          <p className="text-[13px] font-semibold">最近核验：{verifyLabels[latest.status] || latest.status}{latest.status === 'queued' && <span className="font-normal text-muted-foreground">（先等待审批、待办和 Agent 回合处理完毕）</span>}</p>
          <ul className="mt-2 grid gap-1 text-[13px]">
            {latest.observations.map((item, index) => (
              <li key={index} className="flex flex-wrap items-center gap-1.5">
                <StatusChip tone={verifyTones[item.status] || 'default'}>{verifyLabels[item.status] || item.status}</StatusChip>
                <span>{item.system || item.device_id || item.ticket_id || '核验服务'}：{item.reason || `当前状态 ${item.actual ?? '无'}，期望 ${item.expected ?? item.role}`}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      <Row>
        {checks.length > 0 && room.status === 'open' && room.can_write !== false && (
          <Button type="button" size="sm" variant="secondary" disabled={busy.verify || ['queued', 'running'].includes(latest?.status ?? '')} onClick={() => act('verify')}>重新核验</Button>
        )}
        {canManage && room.status === 'open' && !draft && <Button type="button" size="sm" variant="outline" onClick={() => setDraft(JSON.parse(JSON.stringify(verification.goal)))}>配置核验条件</Button>}
      </Row>
      {draft && (
        <form className="flex flex-col gap-3 rounded-md bg-muted p-3" onSubmit={event => { event.preventDefault(); save() }}>
          {draft.checks.map((check, index) => (
            <div key={index} className="flex flex-col gap-2 rounded-md border border-border bg-card p-3">
              {'system' in check ? (
                <div className="grid gap-2 sm:grid-cols-2">
                  <SelectInput label="系统" value={check.system} onChange={system => setCheck(index, { ...check, system, role: roles(system)[0] ?? '' })}
                    options={verification.catalog.map(item => ({ id: item.system, label: item.system }))} />
                  <SelectInput label="最低权限" value={check.role} onChange={role => setCheck(index, { ...check, role })} options={roles(check.system).map(role => ({ id: role, label: role }))} />
                </div>
              ) : 'device_id' in check ? (
                <TextInput label="已锁定的设备编号" value={check.device_id} onChange={device_id => setCheck(index, { ...check, device_id })} isRequired />
              ) : (
                <TextInput label="应保持打开的工单编号" value={check.ticket_id} onChange={ticket_id => setCheck(index, { ...check, ticket_id })} isRequired />
              )}
              <div><Button type="button" size="sm" variant="ghost" className="text-destructive-foreground" onClick={() => setDraft({ ...draft, checks: draft.checks.filter((_, i) => i !== index) })}><Trash2Icon className="size-3.5" />移除条件</Button></div>
            </div>
          ))}
          <Row>
            <Button type="button" size="sm" variant="outline" disabled={draft.checks.length >= 10 || !verification.catalog.length}
              onClick={() => { const system = verification.catalog[0]?.system; if (system) addCheck({ kind: 'access_level', system, role: roles(system)[0] ?? '' }) }}><PlusIcon className="size-3.5" />权限条件</Button>
            <Button type="button" size="sm" variant="outline" disabled={draft.checks.length >= 10} onClick={() => addCheck({ kind: 'device_locked', device_id: '' })}><PlusIcon className="size-3.5" />设备锁定条件</Button>
            <Button type="button" size="sm" variant="outline" disabled={draft.checks.length >= 10} onClick={() => addCheck({ kind: 'ticket_open', ticket_id: '' })}><PlusIcon className="size-3.5" />工单条件</Button>
          </Row>
          <SelectInput label="未通过时，自动只读跟进的最多轮数" value={String(draft.replan_limit)} onChange={value => setDraft({ ...draft, replan_limit: Number(value) })}
            options={[{ id: '0', label: '0（直接人工跟进）' }, { id: '1', label: '1' }, { id: '2', label: '2' }]} />
          {!draft.checks.length && <Muted>保存空条件后，该事项将使用人工验收。</Muted>}
          <Row><Button size="sm" type="submit" disabled={busy.goal}>保存并核验</Button><Button type="button" size="sm" variant="ghost" onClick={() => setDraft(null)}>取消</Button></Row>
        </form>
      )}
      {checkpoints.length > 0 && (
        <details className="rounded-md border border-border p-3 text-[13px]">
          <summary className="cursor-pointer font-medium">已保存 {checkpoints.length} 个执行检查点</summary>
          <p className="mt-2 text-muted-foreground">中断重试会复用同一回合中已完成的步骤。</p>
          <ul className="mt-1 grid gap-0.5">{checkpoints.map(point => <li key={point.run_id + point.step_key}><code>{point.step_key}</code> · {formatSeconds(point.created_at)}</li>)}</ul>
        </details>
      )}
    </PanelItem>
  )
}

// ---- Collaboration todos ----
interface TodoChange { action: 'cancel' | 'reassign'; assignee: string; reason: string }
export function TodoPanel({ todos, members, reviewers, room, identity, busy, act, clarify }: {
  todos: TaskTodo[]; members: TaskMember[]; reviewers: string[]; room: TaskRoom; identity: Identity | null; busy: Busy; act: TaskAct
  clarify: { approval: Approval; at: number } | null
}) {
  const [question, setQuestion] = useState('')
  const [due, setDue] = useState('')
  const [assignee, setAssignee] = useState(room.requester)
  const [answers, setAnswers] = useState<Record<string, string>>({})
  const [changes, setChanges] = useState<Record<string, TodoChange>>({})
  const questionRef = useRef<HTMLTextAreaElement | null>(null)
  const names = useMemo(() => {
    const result: Record<string, string> = { [room.requester]: room.requester_name }
    if (room.can_write !== false && identity?.subject) result[identity.subject] = identity.username || '我'
    const eventNames: Record<string, string> = Object.fromEntries(room.events.map(event => [event.actor, event.actor_name]))
    for (const id of reviewers || []) result[id] = eventNames[id] || id
    for (const member of members || []) { if (member.role === 'collaborator') result[member.subject] = member.profile.username; else delete result[member.subject] }
    return result
  }, [room, identity, reviewers, members])
  const people = Object.entries(names).map(([id, label]) => ({ id, label }))
  const changeOf = (id: string): TodoChange => changes[id] ?? { action: 'cancel', assignee: room.requester, reason: '' }
  const setChange = (id: string, patch: Partial<TodoChange>) => setChanges(current => ({ ...current, [id]: { ...changeOf(id), ...patch } }))

  useEffect(() => {
    if (!clarify) return
    setQuestion(`关于申请 ${clarify.approval.approval_id.slice(0, 12)}，请补充说明：`)
    setAssignee(room.requester)
    setTimeout(() => { questionRef.current?.focus(); questionRef.current?.scrollIntoView({ block: 'center', behavior: 'smooth' }) }, 150)
  }, [clarify])  // eslint-disable-line react-hooks/exhaustive-deps

  async function create() {
    if (await act('todos', { question, assignee, due_at: due ? new Date(due).getTime() / 1000 : null })) { setQuestion(''); setDue('') }
  }
  async function answer(todo: TaskTodo) {
    if (await act('todos/' + todo.id + '/answer', { answer: answers[todo.id] })) setAnswers(current => ({ ...current, [todo.id]: '' }))
  }
  const open = todos.filter(todo => todo.status === 'open').length
  return (
    <PanelItem id="todos" title="协作待办" badge={<Count n={open} tone="warning" />}>
      {todos.map(todo => (
        <Item key={todo.id}>
          <Row>
            <StatusChip tone={todo.status === 'open' ? 'warning' : todo.status === 'cancelled' ? 'default' : 'success'}>{todo.status === 'open' ? '待答复' : todo.status === 'cancelled' ? '已取消' : '已答复'}</StatusChip>
            <span className="text-[13px] text-muted-foreground">回答人 {names[todo.assignee] || todo.assignee}</span>
            {todo.due_at && <span className="text-[13px] text-muted-foreground">· 截止 {formatSeconds(todo.due_at)}</span>}
          </Row>
          <p className="mt-2">{todo.question}</p>
          {todo.answer && <p className="mt-2 rounded-lg bg-muted p-2 text-[13px]">{todo.answer}</p>}
          {todo.status === 'open' && todo.assignee === identity?.subject && room.status === 'open' && (
            <form className="mt-3 flex flex-col gap-2" onSubmit={event => { event.preventDefault(); answer(todo) }}>
              <TextAreaInput ariaLabel="答复" value={answers[todo.id] || ''} onChange={value => setAnswers(current => ({ ...current, [todo.id]: value }))} isRequired maxLength={8000} placeholder="补充资料或回答问题" rows={2} />
              <div><Button size="sm" type="submit" disabled={busy['todos/' + todo.id + '/answer']}>提交答复并请 Agent 跟进</Button></div>
            </form>
          )}
          {todo.status === 'open' && room.can_write !== false && (todo.creator === identity?.subject || identity?.roles?.includes('it_admin')) && (
            <form className="mt-3 grid gap-2 sm:grid-cols-2" onSubmit={event => { event.preventDefault(); act('todos/' + todo.id + '/change', { ...changeOf(todo.id) }) }}>
              <SelectInput ariaLabel="调整方式" value={changeOf(todo.id).action} onChange={action => setChange(todo.id, { action: action as TodoChange['action'] })}
                options={[{ id: 'cancel', label: '取消待办' }, { id: 'reassign', label: '改派待办' }]} />
              {changeOf(todo.id).action === 'reassign' && <SelectInput ariaLabel="改派给" value={changeOf(todo.id).assignee} onChange={value => setChange(todo.id, { assignee: value })} options={people} isRequired />}
              <TextInput ariaLabel="原因" className="sm:col-span-2" value={changeOf(todo.id).reason} onChange={reason => setChange(todo.id, { reason })} isRequired maxLength={8000} placeholder="取消或改派原因" />
              <div><Button size="sm" variant="outline" type="submit" disabled={busy['todos/' + todo.id + '/change']}>提交调整</Button></div>
            </form>
          )}
        </Item>
      ))}
      {!todos.length && <Muted>还没有协作待办。需要他人补充资料时，在下方发起。</Muted>}
      {room.status === 'open' && room.can_write !== false && (
        <form className="flex flex-col gap-3 rounded-md bg-muted p-3" onSubmit={event => { event.preventDefault(); create() }}>
          <TextAreaInput label="问题" value={question} onChange={setQuestion} isRequired maxLength={8000} rows={2} inputRef={questionRef} />
          <div className="grid gap-2 sm:grid-cols-2">
            <SelectInput label="回答人" value={assignee} onChange={setAssignee} options={people} isRequired />
            <TextInput label="截止时间（可选）" type="datetime-local" value={due} onChange={setDue} />
          </div>
          <div><Button size="sm" type="submit" disabled={busy.todos}><PlusIcon className="size-3.5" />发起补充待办</Button></div>
        </form>
      )}
    </PanelItem>
  )
}

// ---- Reminders ----
const reminderLabels: Record<string, string> = { pending: '待触发', fired: '已触发', cancelled: '已取消' }
export function ReminderPanel({ reminders, room, canManage, busy, act }: { reminders: TaskReminder[]; room: TaskRoom; canManage: boolean; busy: Busy; act: TaskAct }) {
  const [text, setText] = useState('')
  const [due, setDue] = useState('')
  const [wake, setWake] = useState(false)
  async function create() { if (await act('reminders', { text, due_at: new Date(due).getTime() / 1000, wake })) { setText(''); setDue('') } }
  return (
    <PanelItem id="reminders" title="定时提醒与复查" badge={<Count n={reminders.filter(r => r.status === 'pending').length} />}>
      <Muted>到期消息保存在本事项中；勾选复查后会唤醒 Agent 做只读跟进。</Muted>
      {reminders.map(timer => (
        <div key={timer.id} className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border p-2.5">
          <div className="min-w-0">
            <p>{timer.text}</p>
            <p className="text-[13px] text-muted-foreground">{formatSeconds(timer.due_at)}</p>
          </div>
          <Row>
            <StatusChip tone={timer.status === 'pending' ? 'accent' : 'default'}>{reminderLabels[timer.status] || timer.status}</StatusChip>
            {canManage && timer.status === 'pending' && <Button type="button" size="sm" variant="ghost" disabled={busy['reminders/' + timer.id + '/cancel']} onClick={() => act('reminders/' + timer.id + '/cancel', {})}>取消</Button>}
          </Row>
        </div>
      ))}
      {canManage && room.status === 'open' && (
        <form className="flex flex-col gap-3 rounded-md bg-muted p-3" onSubmit={event => { event.preventDefault(); create() }}>
          <TextInput label="提醒内容" value={text} onChange={setText} isRequired maxLength={8000} placeholder="提醒内容或复查目标" />
          <TextInput label="到期时间" type="datetime-local" value={due} onChange={setDue} isRequired />
          <CheckInput isSelected={wake} onChange={setWake}>到期请 Agent 只读复查</CheckInput>
          <div><Button size="sm" type="submit" disabled={busy.reminders}>安排提醒</Button></div>
        </form>
      )}
    </PanelItem>
  )
}

// ---- Runs and budget ----
const attemptLabels: Record<string, string> = { running: '运行中', done: '已完成', failed: '失败', interrupted: '中断' }
const attemptTones: Record<string, Tone> = { running: 'accent', done: 'success', failed: 'danger', interrupted: 'warning' }
export function RunsPanel({ budget, attempts, isAdmin, busy, act }: { budget: TaskBudget; attempts: TaskAttempt[]; isAdmin: boolean; busy: Busy; act: TaskAct }) {
  const [tokens, setTokens] = useState(String(budget.token_limit))
  const [runs, setRuns] = useState(String(budget.run_limit))
  return (
    <PanelItem id="runs" title="运行记录与累计预算">
      <div className="grid gap-3 sm:grid-cols-2">
        <Gauge value={budget.run_limit ? (budget.used_runs / budget.run_limit) * 100 : 0} tone={budget.used_runs >= budget.run_limit ? 'danger' : 'default'}
          label="运行次数" valueText={`${budget.used_runs} / ${budget.run_limit}`} />
        <Gauge value={budget.token_limit ? (budget.used_tokens / budget.token_limit) * 100 : 0} tone={budget.used_tokens >= budget.token_limit ? 'danger' : 'default'}
          label="已知 token" valueText={`${budget.used_tokens} / ${budget.token_limit}`} />
      </div>
      <Muted>达到预算后暂停后续运行。正在执行的回合可能超出 token 阈值；中断或失败时的未报告消耗不计入已知 token。</Muted>
      {isAdmin && (
        <form className="grid gap-2 rounded-md bg-muted p-3 sm:grid-cols-2" onSubmit={event => { event.preventDefault(); act('budget', { token_limit: Number(tokens), run_limit: Number(runs) }) }}>
          <TextInput label="token 上限" type="number" min={1} max={10000000} value={tokens} onChange={setTokens} isRequired />
          <TextInput label="运行次数上限" type="number" min={1} max={10000} value={runs} onChange={setRuns} isRequired />
          <div className="sm:col-span-2"><Button size="sm" type="submit" disabled={busy.budget}>调整预算并继续排队</Button></div>
        </form>
      )}
      {attempts.map(attempt => (
        <Item key={attempt.id}>
          <Row>
            <StatusChip tone={attemptTones[attempt.status] || 'default'}>{attemptLabels[attempt.status] || attempt.status}</StatusChip>
            <span className="text-[13px] text-muted-foreground">第 {attempt.attempt} 次尝试 · 排队 {Math.max(0, attempt.started_at - attempt.queued_at).toFixed(1)} 秒{attempt.ended_at ? ` · 运行 ${(attempt.ended_at - attempt.started_at).toFixed(1)} 秒` : ''}</span>
          </Row>
          <p className="mt-1 text-xs text-muted-foreground">触发消息 <code>{attempt.run_id.slice(0, 12)}</code></p>
          {attempt.error && <p className="mt-1 text-[13px] text-destructive-foreground">原因：{attempt.error}</p>}
          {attempt.tools.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1">
              {attempt.tools.map((tool, index) => <Badge key={index} variant={tool.success ? 'secondary' : 'destructive'} className="font-mono">{tool.tool_name}</Badge>)}
            </div>
          )}
        </Item>
      ))}
    </PanelItem>
  )
}

// ---- Files ----
export function FilesPanel({ files, room, client, onChanged }: { files: TaskFile[]; room: TaskRoom; client: Pick<TaskClient, 'upload' | 'download'>; onChanged: () => void }) {
  const [file, setFile] = useState<File | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const input = useRef<HTMLInputElement | null>(null)
  const requestId = useRef('')
  const alive = useRef(true)
  useEffect(() => () => { alive.current = false }, [])
  // A new file gets a new request key; re-uploading the same selection after a lost response reuses it.
  function choose(event: React.ChangeEvent<HTMLInputElement>) { setFile(event.target.files?.[0] ?? null); requestId.current = crypto.randomUUID() }
  async function upload() {
    if (!file) return
    if (file.size > 5 * 1024 * 1024) { setError('附件上限为 5 MB'); return }
    setBusy(true); setError('')
    try { await client.upload(room.id, file, requestId.current); if (alive.current) { setFile(null); if (input.current) input.current.value = ''; onChanged() } }
    catch (e) { if (alive.current) setError(errorText(e)) } finally { if (alive.current) setBusy(false) }
  }
  async function download(item: TaskFile) {
    try {
      const blob = await client.download(room.id, item.id)
      if (!alive.current) return
      const url = URL.createObjectURL(blob), link = document.createElement('a')
      link.href = url; link.download = item.name; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (e) { if (alive.current) setError(errorText(e)) }
  }
  return (
    <PanelItem id="files" title="附件与证据材料" badge={<Count n={files.length} />}>
      {error && <ErrorAlert>{error}</ErrorAlert>}
      {files.map(item => (
        <div key={item.id} className="flex items-center justify-between gap-2 rounded-md border border-border p-2.5">
          <div className="min-w-0"><p className="truncate">{item.name}</p><p className="text-[13px] text-muted-foreground">{Math.ceil(item.size / 1024)} KB · 可在步骤中引用对应上传消息</p></div>
          <Button type="button" size="icon" variant="ghost" aria-label={`下载 ${item.name}`} onClick={() => download(item)}><DownloadIcon className="size-4" /></Button>
        </div>
      ))}
      {room.can_write !== false && room.status === 'open' && (
        <form className="flex flex-col gap-2 rounded-md bg-muted p-3" onSubmit={event => { event.preventDefault(); upload() }}>
          <input ref={input} type="file" required accept=".txt,.log,.csv,.json,.png,.jpg,.jpeg,.pdf" onChange={choose}
            className="text-[13px] file:mr-3 file:rounded-lg file:border-0 file:bg-accent file:px-3 file:py-1.5 file:text-[13px] file:font-medium file:text-foreground" />
          <Muted>单个附件最多 5 MB。下载遵循事项成员权限。</Muted>
          <div><Button size="sm" type="submit" aria-busy={busy} disabled={(busy || !file) || (busy)}>上传材料</Button></div>
        </form>
      )}
    </PanelItem>
  )
}

// ---- Members and reviewer assignment ----
const memberLabels: Record<string, string> = { collaborator: '协作者', observer: '观察者', revoked: '已移除' }
type MembersProps = {
  members: TaskMember[]; reviewers: string[]; room: TaskRoom; client: Pick<TaskClient, 'directory'>; canManage: boolean; isAdmin: boolean; busy: Busy; act: TaskAct
}
export function MembersPanel(props: MembersProps) {
  return <PanelItem id="members" title="参与人与审批指派" badge={<Count n={props.members.filter(m => m.role !== 'revoked').length} />}>
    <MembersEditor {...props} />
  </PanelItem>
}
export function MembersEditor({ members, reviewers, room, client, canManage, isAdmin, busy, act }: MembersProps) {
  const [directory, setDirectory] = useState<DirectoryPerson[]>([])
  const [query, setQuery] = useState('')
  const [loading, setLoading] = useState(false)
  const [notice, setNotice] = useState('')
  const [subject, setSubject] = useState('')
  const [role, setRole] = useState('collaborator')
  const [selected, setSelected] = useState<string[]>([...reviewers])
  const [error, setError] = useState('')
  const alive = useRef(true)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  const reviewerKey = [...reviewers].sort().join('\n')
  useEffect(() => { setSelected([...reviewers]) }, [reviewerKey])  // eslint-disable-line react-hooks/exhaustive-deps
  const names: Record<string, string> = Object.fromEntries([...members.map(m => [m.subject, m.profile.username]), ...directory.map(p => [p.subject, p.username])])
  async function load() {
    setLoading(true); setError('')
    try { const result = await client.directory(room.id); if (alive.current) setDirectory(result.items) }
    catch (e) { if (alive.current) setError(errorText(e)) }
    finally { if (alive.current) setLoading(false) }
  }
  useEffect(() => { if (canManage) void load() }, [client, room.id, canManage])
  const available = directory.filter(person => person.subject !== room.requester)
  const matching = available.filter(person => `${person.username} ${person.employee_id || ''} ${person.department}`.toLowerCase().includes(query.trim().toLowerCase()))
  // Keep a selected member visible if the search is edited before submission.
  const options = available.filter(person => person.subject === subject || matching.includes(person))
  async function saveMember() {
    setNotice('')
    if (await act('members', { subject, role }) && alive.current) setNotice(`${names[subject] || subject} 的事项权限已更新。`)
  }
  return (
    <div className="flex flex-col gap-3 text-sm">
      <Muted>仅申请人、受邀成员、指定审批人和本租户 IT 管理员可访问。同部门身份不会自动授予访问权。</Muted>
      {members.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {members.map(member => <Badge key={member.subject} variant={member.role === 'collaborator' ? 'secondary' : 'outline'}>{member.profile.username} · {memberLabels[member.role] || member.role}</Badge>)}
        </div>
      )}
      <p className="text-[13px]">{reviewers.length ? `指定审批人：${reviewers.map(id => names[id] || id).join('、')}` : '未指定审批人；具备审批角色的受邀协作者可以审批。'}</p>
      {error && <ErrorAlert>{error}</ErrorAlert>}
      {notice && <InfoAlert status="success">{notice}</InfoAlert>}
      {canManage && (
        <div className="flex flex-col gap-3 rounded-md bg-muted p-3">
          <div className="flex items-center justify-between gap-2"><Muted>{loading ? '正在读取可选成员…' : '可选择已登录过本租户的成员。'}</Muted><Button type="button" size="sm" variant="ghost" disabled={loading} onClick={load}>刷新成员</Button></div>
          <TextInput label="查找成员" value={query} onChange={setQuery} placeholder="输入姓名、工号或部门" />
          {!loading && !matching.length && <Muted>{query ? '没有匹配成员。请调整搜索词，或请同事登录后刷新。' : '暂无可添加成员，请同事登录后刷新。'}</Muted>}
          <form className="grid gap-2 sm:grid-cols-2" onSubmit={event => { event.preventDefault(); void saveMember() }}>
            <SelectInput label="成员" value={subject} onChange={setSubject} placeholder="选择成员" isRequired
              options={options.map(person => ({ id: person.subject, label: `${person.username}${person.employee_id ? ` · ${person.employee_id}` : ''} · ${person.department}` }))} />
            <SelectInput label="权限" value={role} onChange={setRole} options={[{ id: 'collaborator', label: '协作者' }, { id: 'observer', label: '观察者' }, { id: 'revoked', label: '移除访问' }]} />
            <div className="sm:col-span-2"><Button size="sm" type="submit" disabled={busy.members || !subject}>更新成员</Button></div>
          </form>
          {isAdmin && (
            <form className="flex flex-col gap-2" onSubmit={event => { event.preventDefault(); act('reviewers', { subjects: selected }) }}>
              <MultiSelectInput label="审批负责人（指定后获得事项访问权）" values={selected} onChange={setSelected}
                options={directory.filter(p => p.roles.some(r => ['approver', 'it_admin'].includes(r)) && p.subject !== room.requester).map(p => ({ id: p.subject, label: p.username }))} />
              <div><Button size="sm" type="submit" disabled={busy.reviewers}>保存审批指派</Button></div>
            </form>
          )}
        </div>
      )}
    </div>
  )
}

// ---- Notifications ----
const deliveryLabels: Record<string, string> = { queued: '等待投递', sending: '投递中', sent: '已投递', failed: '投递失败', cancelled: '已取消' }
export function NotificationsPanel({ notifications, members, canManage, busy, act }: { notifications: TaskNotifications; members: TaskMember[]; canManage: boolean; busy: Busy; act: TaskAct }) {
  const [channel, setChannel] = useState(notifications.subscription?.channel || notifications.channels[0] || 'email')
  const [enabled, setEnabled] = useState(!!notifications.subscription?.enabled)
  const [escalation, setEscalation] = useState(notifications.subscription?.escalate_to || '')
  return (
    <PanelItem id="notifications" title="站外通知与超时升级">
      {!notifications.channels.length ? <Muted>管理员尚未为当前身份配置邮件或飞书通知地址。</Muted> : (
        <form className="flex flex-col gap-3" onSubmit={event => { event.preventDefault(); act('notifications', { channel, enabled, escalate_to: escalation || null }) }}>
          <CheckInput isSelected={enabled} onChange={setEnabled}>订阅事项更新</CheckInput>
          <SelectInput label="渠道" value={channel} onChange={setChannel} options={notifications.channels.map(name => ({ id: name, label: name === 'email' ? '邮件' : '飞书' }))} />
          {canManage && (
            <SelectInput label="超时升级给（选填）" value={escalation || 'none'} onChange={value => setEscalation(value === 'none' ? '' : value)}
              options={[{ id: 'none', label: '不升级' }, ...members.filter(m => m.role !== 'revoked').map(person => ({ id: person.subject, label: person.profile.username }))]} />
          )}
          <Muted>仅发送订阅后的更新；到期事件可通知升级对象。对方需已配置同一通知渠道。</Muted>
          <div><Button size="sm" type="submit" disabled={busy.notifications}>保存通知设置</Button></div>
        </form>
      )}
      {notifications.deliveries.map(delivery => (
        <p key={delivery.id} className="text-[13px]">{delivery.channel} · {deliveryLabels[delivery.status] || delivery.status} · 尝试 {delivery.attempts} 次{delivery.error && <span className="text-destructive-foreground">（{delivery.error}）</span>}</p>
      ))}
    </PanelItem>
  )
}
