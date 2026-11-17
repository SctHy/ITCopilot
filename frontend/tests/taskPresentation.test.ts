import { taskEvent, taskRoom, workspace as taskWorkspace } from './fixtures.ts'
import type { TaskProgress, TaskSuggestion } from '../src/lib/types.ts'
import { test } from 'vitest'
import assert from 'node:assert/strict'
import { groupTaskEvents, progressChanges, taskNextAction } from '../src/lib/taskPresentation.ts'

const room = taskRoom('room')
const data = {
  ...taskWorkspace('排查访问'),
  progress: { version: 2, goal: '排查访问', steps: [], next_action: '核对账号' },
  members: [{ subject: 'bob', role: 'collaborator', profile: { username: 'Bob' } }],
  todos: [{ id: '1', creator: 'alice', assignee: 'alice', question: '提供截图', status: 'open', due_at: 10 },
    { id: '2', creator: 'alice', assignee: 'bob', question: '核对成员', status: 'open', due_at: 20 },
    { id: '3', creator: 'alice', assignee: 'bob', question: '网络情况', status: 'answered' }]
}

test('pending work identifies its owner, prioritizes the viewer and preserves server ordering', () => {
  const next = taskNextAction(room, data, 'bob')
  assert.deepEqual(next.todos.map(todo => [todo.id, todo.name, todo.mine]), [['2', 'Bob', true], ['1', 'Alice', false]])
  assert.equal(next.panel, 'todos')
  assert.deepEqual(data.todos.map(todo => todo.id), ['1', '2', '3'])
})

test('closed rooms do not advertise leftover todos or AI suggestions as work to perform', () => {
  for (const status of ['resolved', 'stopped'] as const) {
    const next = taskNextAction({ ...room, status }, data, 'bob')
    assert.deepEqual(next.todos, [])
    assert.equal(next.panel, 'progress')
  }
})

test('approval, failure and budget phases are not masked by ordinary collaboration todos', () => {
  for (const [phase, panel] of [['awaiting_confirmation', 'approvals'], ['awaiting_approval', 'approvals'], ['manual_review', 'approvals'], ['executing', 'approvals'], ['budget_paused', 'runs'], ['failed', 'runs']] as const) {
    assert.equal(taskNextAction({ ...room, phase }, data, 'alice').panel, panel)
  }
})

test('stale or dismissed suggestions never replace the current accepted next step', () => {
  const workspace = { ...data, todos: [], suggestions: [
    { id: 'pending', status: 'pending', base_version: 1, payload: { ...data.progress, next_action: '旧建议' } },
    { id: 'dismissed', status: 'dismissed', base_version: 2, payload: { ...data.progress, next_action: '已忽略' } }
  ] satisfies TaskSuggestion[] }
  assert.equal(taskNextAction(room, workspace).text, '核对账号')
  workspace.suggestions.push({ id: 'pending', status: 'pending', base_version: 2, payload: { ...data.progress, next_action: '新建议' } })
  const next = taskNextAction(room, workspace)
  assert.equal(next.text, '新建议')
  assert.match(next.title, /待采纳/)
})

test('collapsing bookkeeping preserves order and keeps approval decisions, answers and failures visible', () => {
  const kinds = ['requester', 'member', 'progress', 'suggestion', 'approval_vote', 'todo_answer', 'system', 'progress', 'suggestion', 'assistant']
  const events = kinds.map((kind, id) => taskEvent(String(id), id, kind))
  const groups = groupTaskEvents(events)
  assert.deepEqual(groups.flat(), events)
  for (const kind of ['approval_vote', 'todo_answer', 'system']) assert.equal(groups.find(group => group.some(event => event.kind === kind))?.length, 1)
  assert.deepEqual(groups.filter(group => group.length > 1).map(group => group.map(event => event.kind)), [['member', 'progress', 'suggestion'], ['progress', 'suggestion']])
})

test('progress comparison exposes removed requirements and completion changes before acceptance', () => {
  const current: TaskProgress = { version: 1, goal: '恢复访问', blocker: '等待网络情况', next_action: '补充截图', steps: [
    { title: '核对账号', status: 'working', evidence_ids: ['a'] },
    { title: '实际登录验证', status: 'todo', evidence_ids: [] }
  ] }
  const proposed: TaskProgress = { version: 2, goal: '恢复访问', blocker: '', next_action: '确认角色', steps: [
    { title: '核对账号', status: 'done', evidence_ids: ['a', 'b'] }
  ] }
  const before = JSON.stringify(current)
  const changes = progressChanges(current, proposed)
  assert.ok(changes.some(change => change.label === '阻塞原因' && change.after === ''))
  assert.ok(changes.some(change => change.label === '移除步骤' && change.before === '实际登录验证'))
  assert.ok(changes.some(change => change.label === '核对账号' && change.after === '有完成证据'))
  assert.equal(JSON.stringify(current), before)
})

test('unchanged evidence order creates no spurious progress change', () => {
  const current: TaskProgress = { version: 1, goal: '排查', steps: [{ title: '核对', status: 'working', evidence_ids: ['a', 'b'] }] }
  const proposed = { ...current, steps: [{ ...current.steps[0]!, evidence_ids: ['b', 'a'] }] }
  assert.deepEqual(progressChanges(current, proposed), [])
})

test('step reordering is visible and superseded suggestions are not the next action', () => {
  const current: TaskProgress = { version: 1, goal: '排查', steps: [
    { title: '先核对身份', status: 'todo', evidence_ids: [] }, { title: '再核对角色', status: 'todo', evidence_ids: [] }
  ] }
  assert.equal(progressChanges(current, { ...current, steps: [...current.steps].reverse() })[0]?.label, '步骤顺序')
  const next = taskNextAction(room, { ...data, todos: [], suggestions: [{ id: 'superseded', status: 'superseded', base_version: 2, payload: { ...data.progress, next_action: '旧建议' } }] })
  assert.equal(next.text, '核对账号')
})
