import { deferred, requestKey, taskClient, taskEvent, taskItem, taskRoom } from './fixtures.ts'
import type { Approval, Identity, Page, TaskListItem, TaskRoom } from '../src/lib/types.ts'
import type { TaskTransport } from '../src/lib/taskController.ts'
import { test, vi } from 'vitest'
import assert from 'node:assert/strict'
import { approvalPermissions } from '../src/lib/approvalActions.ts'
import { createTaskController } from '../src/lib/taskController.ts'
import { createTaskClient } from '../src/lib/taskClient.ts'

const approval: Approval = { approval_id: 'approval-A', payload_hash: 'a'.repeat(64), status: 'pending', requester: 'alice',
  action_requests: [{ tool_name: 'request_access', arguments: { employee_id: '1001' } }], decisions: [] }
const bob: Identity = { username: 'Bob', department: 'IT', subject: 'bob', employee_id: '2001', roles: ['approver'] }
const room = (id: string): TaskRoom => ({ ...taskRoom(id), approvals: [approval] })
const client = (overrides: Partial<TaskTransport>) => taskClient({ list: async () => ({ items: [] }), get: async id => room(id), post: async () => ({}),
  decide: async () => ({}), ...overrides })

test('approval options respect applicant, target account, repeated vote and final state', () => {
  assert.equal(approvalPermissions(approval, bob).canDecide, true)
  for (const identity of [null, { ...bob, subject: 'alice' }, { ...bob, employee_id: '1001' }, { ...bob, roles: ['employee'] } satisfies Identity]) {
    assert.equal(approvalPermissions(approval, identity).canDecide, false)
  }
  assert.equal(approvalPermissions({ ...approval, decisions: [{ actor: 'bob', decision: 'approved' }] }, bob).canDecide, false)
  assert.equal(approvalPermissions({ ...approval, status: 'rejected' }, bob).canDecide, false)
})

test('room observers and undesignated approvers cannot see voting actions', () => {
  assert.equal(approvalPermissions({ ...approval, room_can_write: false }, bob).canDecide, false)
  assert.equal(approvalPermissions({ ...approval, assigned_reviewers: ['carol'] }, bob).canDecide, false)
  assert.equal(approvalPermissions({ ...approval, assigned_reviewers: ['bob'] }, bob).canDecide, true)
})

test('only the applicant can withdraw a pending request; holders and admins can give access back early', () => {
  const alice: Identity = { username: 'Alice', department: 'IT', subject: 'alice', employee_id: '1001', roles: ['employee'] }
  const erin: Identity = { username: 'Erin', department: 'IT', subject: 'erin', employee_id: '9001', roles: ['it_admin'] }
  assert.equal(approvalPermissions(approval, alice).canWithdraw, true)
  assert.equal(approvalPermissions(approval, bob).canWithdraw, false)
  assert.equal(approvalPermissions({ ...approval, status: 'approved' }, alice).canWithdraw, false)
  const later = new Date(Date.now() + 3600_000).toISOString(), earlier = new Date(Date.now() - 1000).toISOString()
  const active = { ...approval, status: 'approved', revocations: [{ status: 'queued', operation_id: 'grant', not_before: later }] }
  assert.equal(approvalPermissions(active, alice).canRelease, true)
  assert.equal(approvalPermissions(active, erin).canRelease, true)
  assert.equal(approvalPermissions(active, bob).canRelease, false)
  for (const revocations of [[], [{ status: 'succeeded', operation_id: 'grant', not_before: later }], [{ status: 'queued', operation_id: 'grant', not_before: earlier }]]) {
    assert.equal(approvalPermissions({ ...active, revocations }, alice).canRelease, false)
  }
})

test('a draft is the applicant\'s to confirm or cancel; approvers wait for it', () => {
  const alice: Identity = { username: 'Alice', department: 'IT', subject: 'alice', employee_id: '1001', roles: ['employee'] }
  const draft = { ...approval, status: 'draft' }
  assert.equal(approvalPermissions(draft, alice).canConfirm, true)
  assert.equal(approvalPermissions(draft, alice).canWithdraw, true)
  assert.equal(approvalPermissions(draft, bob).canConfirm, false)
  assert.equal(approvalPermissions(draft, bob).canDecide, false)
  assert.match(approvalPermissions(draft, bob).reason, /申请人确认/)
  assert.equal(approvalPermissions(approval, alice).canConfirm, false)
})

test('confirm goes through the transport and refreshes the room', async () => {
  const calls: string[][] = []
  const c = createTaskController(client({ confirm: async a => { calls.push(['confirm', a.approval_id]); return a } }))
  await c.select('room/A')
  await c.decide({ ...approval, status: 'draft' }, 'confirm')
  assert.deepEqual(calls, [['confirm', 'approval-A']])
})

test('withdraw and release go through the transport and refresh the room', async () => {
  const calls: string[][] = []
  const c = createTaskController(client({ withdraw: async a => { calls.push(['withdraw', a.approval_id]); return a },
    release: async a => { calls.push(['release', a.approval_id]); return a } }))
  await c.select('room/A')
  await c.decide(approval, 'withdraw')
  await c.decide(approval, 'release')
  assert.deepEqual(calls, [['withdraw', 'approval-A'], ['release', 'approval-A']])
  c.dispose()
})

test('manual close carries the operator note to the transport', async () => {
  const calls: string[][] = []
  const c = createTaskController(client({ resolve: async (a, note) => { calls.push(['resolve', a.approval_id, note]); return a } }))
  await c.select('room/A')
  await c.decide(approval, 'resolve', '权限已人工保留')
  assert.deepEqual(calls, [['resolve', 'approval-A', '权限已人工保留']])
  c.dispose()
})

test('history refresh preserves loaded overlapping pages but exposes gaps after long absence', async () => {
  let latest = { ...room('room'), events: [taskEvent('a', 1), taskEvent('b', 2)], has_more_events: false }
  const c = createTaskController(client({ get: async () => structuredClone(latest),
    events: async () => ({ items: [taskEvent('d', 4)], has_more: true }) }))
  await c.select('room')
  latest = { ...latest, events: [taskEvent('b', 2), taskEvent('c', 3)], has_more_events: true }
  await c.refresh()
  assert.deepEqual(c.state.room!.events.map(e => e.seq), [1, 2, 3])
  assert.equal(c.state.room!.has_more_events, false)
  latest.events = [taskEvent('e', 5)]
  await c.refresh()
  assert.deepEqual(c.state.room!.events.map(e => e.seq), [5])
  assert.equal(c.state.room!.has_more_events, true)
  await c.older()
  assert.deepEqual(c.state.room!.events.map(e => e.seq), [4, 5])
  c.dispose()
})

test('search changes during an in-flight list discard stale filters and totals', async () => {
  const pending = deferred<Page<TaskListItem>>(), queries: Parameters<TaskTransport['list']>[0][] = []
  const c = createTaskController(client({ list: async params => {
    queries.push(params)
    return queries.length === 1 ? pending.promise : { items: [taskItem('found')], total: 35, has_more: true }
  } }))
  const loading = c.refresh()
  await c.search({ q: 'needle', archived: true, offset: 0 })
  pending.resolve({ items: [taskItem('stale')], total: 1 }); await loading
  assert.equal(c.state.items[0]!.id, 'found')
  assert.equal(c.state.total, 35)
  assert.equal(queries[1]!.q, 'needle')
  assert.equal(queries[1]!.archived, true)
  c.dispose()
})

test('non-decision actions cannot submit formal approval or discussion', async () => {
  let writes = 0
  const c = createTaskController(client({ decide: async () => { writes++ }, post: async () => { writes++ } }))
  await c.select('room')
  await c.decide(approval, 'clarify')
  assert.equal(writes, 0)
  assert.equal(c.state.message, '')
  assert.equal(c.state.room!.approvals![0]!.status, 'pending')
  c.dispose()
})

test('waiting on an approval decision does not block independent discussion', async () => {
  const pending = deferred(), decisions: Parameters<TaskTransport['decide']>[] = [], messages: Parameters<TaskTransport['post']>[] = []
  const c = createTaskController(client({ decide: (...args) => { decisions.push(args); return pending.promise },
    post: async (...args) => { messages.push(args); return {} } }))
  await c.select('room')
  const vote = c.decide(approval, 'approved')
  assert.equal(c.state.approvalBusy[approval.approval_id], true)
  assert.equal(c.state.busy, false)
  c.state.message = '请同时检查访问错误'; await c.send()
  await c.decide(approval, 'approved') // duplicate click while in flight
  assert.equal(decisions.length, 1)
  assert.equal(messages.length, 1)
  assert.equal(decisions[0]![0], 'room')
  assert.equal(decisions[0]![1].payload_hash, approval.payload_hash)
  pending.resolve(); await vote; c.dispose()
})

test('switching rooms discards an old approval error and reloads the selected room', async () => {
  const pending = deferred()
  const c = createTaskController(client({ decide: () => pending.promise }))
  await c.select('old')
  const vote = c.decide(approval, 'rejected')
  await c.select('new')
  pending.reject(new Error('old room failure')); await vote
  assert.equal(c.state.room!.id, 'new')
  assert.deepEqual(c.state.approvalErrors, {})
  c.dispose()
})

test('a selection during a pending refresh immediately schedules the new room read', async () => {
  const pending = deferred<TaskRoom>(), reads: string[] = []
  const c = createTaskController(client({ get: id => { reads.push(id); return id === 'old' ? pending.promise : Promise.resolve(room(id)) } }))
  const loading = c.select('old')
  await c.select('new')
  pending.resolve(room('old')); await loading
  assert.equal(c.state.room!.id, 'new')
  assert.deepEqual(reads, ['old', 'new'])
  c.dispose()
})

test('ambiguous message delivery retains the request key on retry', async () => {
  const sent: unknown[] = []
  const c = createTaskController(client({ post: async (_id, data) => {
    sent.push(data)
    if (sent.length === 1) throw new Error('lost response')
    return {}
  } }))
  await c.select('room'); c.state.message = '补充需求'
  await c.send(); await c.send()
  assert.equal(requestKey(sent[0]), requestKey(sent[1]))
  assert.equal(c.state.message, '')
  c.dispose()
})

test('approval transport binds the selected approval hash and snapshots the caller identity', async () => {
  const settings = { backend: 'python', endpoints: { python: 'http://api.test' }, accessToken: 'bob-token', conversationId: '' }
  const calls: Request[] = []
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, options) => {
    calls.push(new Request(url, options)); return new Response(JSON.stringify({ status: 'approved' }))
  })
  const transport = createTaskClient(settings)
  settings.accessToken = 'alice-token'; settings.endpoints.python = 'http://other.test'
  await transport.decide('room/A', approval, 'approved')
  assert.equal(calls[0]!.url, 'http://api.test/tasks/room%2FA/approvals/approval-A')
  assert.equal(calls[0]!.headers.get('Authorization'), 'Bearer bob-token')
  assert.deepEqual(await calls[0]!.json(), { decision: 'approved', payload_hash: approval.payload_hash })
})

test('a transient poll failure keeps the open room; losing access closes it', async () => {
  let failure: Error | null = null
  const c = createTaskController(client({ get: async id => { if (failure) throw failure; return room(id) } }))
  await c.select('room')
  failure = Object.assign(new Error('502 Bad Gateway'), { status: 502 })
  await c.refresh()
  assert.equal(c.state.room?.id, 'room')
  assert.equal(c.state.error, '502 Bad Gateway')
  failure = null; await c.refresh()
  assert.equal(c.state.error, '')
  failure = Object.assign(new Error('404 Not Found'), { status: 404 })
  await c.refresh()
  assert.equal(c.state.room, null)
  c.dispose()
})
