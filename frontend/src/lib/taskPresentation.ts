import type { TaskEvent, TaskProgress, TaskRoom, TaskWorkspaceData } from './types.ts'

export function progressChanges(current: TaskProgress, proposed: TaskProgress) {
  const changes: { label: string; before: string; after: string }[] = []
  const field = (label: string, before = '', after = '') => { if (before !== after) changes.push({ label, before, after }) }
  field('目标与完成标准', current.goal, proposed.goal)
  field('阻塞原因', current.blocker, proposed.blocker)
  field('下一步', current.next_action, proposed.next_action)
  const labels: Record<string, string> = { todo: '待处理', working: '进行中', done: '有完成证据' }
  const remaining = [...current.steps]
  for (const step of proposed.steps) {
    const index = remaining.findIndex(old => old.title === step.title)
    if (index < 0) { changes.push({ label: '新增步骤', before: '', after: `${step.title} · ${labels[step.status] || step.status}` }); continue }
    const old = remaining.splice(index, 1)[0]!
    field(step.title, labels[old.status] || old.status, labels[step.status] || step.status)
    if (JSON.stringify([...old.evidence_ids].sort()) !== JSON.stringify([...step.evidence_ids].sort()))
      changes.push({ label: `${step.title} · 证据`, before: `${old.evidence_ids.length} 条引用`, after: `${step.evidence_ids.length} 条引用（已更新）` })
  }
  for (const step of remaining) changes.push({ label: '移除步骤', before: step.title, after: '' })
  if (!changes.length && current.steps.map(s => s.title).join('\n') !== proposed.steps.map(s => s.title).join('\n'))
    changes.push({ label: '步骤顺序', before: current.steps.map(s => s.title).join(' → '), after: proposed.steps.map(s => s.title).join(' → ') })
  return changes
}

// Only routine bookkeeping is collapsed. Approval decisions, answers and failures remain visible.
const routineEvents = new Set(['member', 'progress', 'suggestion', 'reviewer_assignment'])
export function groupTaskEvents(events: TaskEvent[]): TaskEvent[][] {
  const groups: TaskEvent[][] = []
  for (const event of events) {
    const previous = groups.at(-1)
    if (routineEvents.has(event.kind) && previous && routineEvents.has(previous[0]!.kind)) previous.push(event)
    else groups.push([event])
  }
  return groups
}

export function taskNextAction(room: TaskRoom, data: TaskWorkspaceData, subject?: string) {
  const names: Record<string, string> = Object.fromEntries(room.events.map(event => [event.actor, event.actor_name]))
  names[room.requester] = room.requester_name
  for (const member of data.members || []) names[member.subject] = member.profile.username
  const todos = room.status === 'open' ? data.todos.filter(todo => todo.status === 'open')
    .sort((a, b) => Number(b.assignee === subject) - Number(a.assignee === subject) || (a.due_at ?? Infinity) - (b.due_at ?? Infinity))
    .map(todo => ({ ...todo, name: names[todo.assignee] || todo.assignee, mine: todo.assignee === subject })) : []
  const result = (title: string, text: string, panel: string | null = null, label = '查看详情') => ({ title, text, panel, label, todos })
  if (room.status === 'resolved') return result('事项已解决', data.progress.resolution || '处理结果和验收记录已保存。', 'progress', '查看验收记录')
  if (room.status === 'stopped') return result('Agent 已停止跟进', '已有审批与业务操作仍按原工作流处理。', 'progress', '查看事项状态')
  if (room.phase === 'awaiting_confirmation') return result(`等待 ${room.requester_name} 确认申请`, '请申请人在审批卡片中核对操作、范围和时长后确认提交。', 'approvals', '查看申请')
  if (room.phase === 'awaiting_approval') return result('等待审批人处理', '有审批权限的参与人可在审批卡片中查看依据并作出正式决定。', 'approvals', '查看审批')
  if (room.phase === 'executing') return result('系统正在执行', '执行结果会保留在事项中。', 'approvals', '查看执行状态')
  if (room.phase === 'manual_review') return result('等待人工核对', '请有处理权限的人员核对执行证据并处理审批卡片。', 'approvals', '查看待核对操作')
  if (room.phase === 'budget_paused') return result('等待管理员调整预算', '累计预算已用完，请联系 IT 管理员处理。', 'runs', '查看运行预算')
  if (room.phase === 'failed') return result('Agent 回合处理失败', '请查看失败记录；可在讨论区重新处理。', 'runs', '查看失败记录')
  if (room.phase === 'working') return result('Agent 正在处理', '可以继续补充信息，或离开页面等待结果。')
  if (todos.length) return result(todos.some(todo => todo.mine) ? '有待你补充的信息' : '等待参与人补充', `还有 ${todos.length} 项待办，回答后可继续跟进。`, 'todos', '查看并处理待办')
  const suggestion = data.suggestions?.find(item => item.status === 'pending' && item.base_version === data.progress.version && item.payload.next_action)
  if (suggestion) return result('Agent 建议的下一步 · 待采纳', suggestion.payload.next_action!, 'suggestions', '查看进度建议')
  if (data.progress.next_action) return result('下一步', data.progress.next_action, 'progress', '查看任务进度')
  return result('等待补充需求', '在讨论区描述接下来需要处理什么；需要他人补充时，可添加参与人并发起协作待办。', 'todos', '查看协作待办')
}
