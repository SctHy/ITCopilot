import { test } from 'node:test'
import assert from 'node:assert/strict'
import { approvalPermissions } from '../src/lib/approvalActions.js'
import { createTaskController } from '../src/lib/taskController.js'
import { createTaskClient } from '../src/lib/taskClient.js'

const approval = { approval_id: 'approval-A', payload_hash: 'a'.repeat(64), status: 'pending', requester: 'alice',
  action_requests: [{ arguments: { employee_id: '1001' } }], decisions: [] }
const bob = { subject: 'bob', employee_id: '2001', roles: ['approver'] }
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }
const room = id => ({ id, title: id, events: [], approvals: [approval] })
const client = overrides => ({ list: async () => ({ items: [] }), get: async id => room(id), post: async () => ({}),
  decide: async () => ({}), ...overrides })

test('approval options respect applicant, target account, repeated vote and final state', () => {
  assert.equal(approvalPermissions(approval, bob).canDecide, true)
  for (const identity of [null, { ...bob, subject: 'alice' }, { ...bob, employee_id: '1001' }, { ...bob, roles: ['employee'] }]) {
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

test('history refresh preserves loaded overlapping pages but exposes gaps after long absence', async () => {
  let latest = { ...room('room'), events: [{ id: 'a', seq: 1 }, { id: 'b', seq: 2 }], has_more_events: false }
  const c = createTaskController(client({ get: async () => structuredClone(latest),
    events: async () => ({ items: [{ id: 'd', seq: 4 }], has_more: true }) }))
  await c.select('room')
  latest = { ...latest, events: [{ id: 'b', seq: 2 }, { id: 'c', seq: 3 }], has_more_events: true }
  await c.refresh()
  assert.deepEqual(c.state.room.events.map(e => e.seq), [1, 2, 3])
  assert.equal(c.state.room.has_more_events, false)
  latest.events = [{ id: 'e', seq: 5 }]
  await c.refresh()
  assert.deepEqual(c.state.room.events.map(e => e.seq), [5])
  assert.equal(c.state.room.has_more_events, true)
  await c.older()
  assert.deepEqual(c.state.room.events.map(e => e.seq), [4, 5])
  c.dispose()
})

test('search changes during an in-flight list discard stale filters and totals', async () => {
  const pending = deferred(), queries = []
  const c = createTaskController(client({ list: async params => {
    queries.push(params)
    return queries.length === 1 ? pending.promise : { items: [{ id: 'found' }], total: 35, has_more: true }
  } }))
  const loading = c.refresh()
  await c.search({ q: 'needle', archived: true, offset: 0 })
  pending.resolve({ items: [{ id: 'stale' }], total: 1 }); await loading
  assert.equal(c.state.items[0].id, 'found')
  assert.equal(c.state.total, 35)
  assert.equal(queries[1].q, 'needle')
  assert.equal(queries[1].archived, true)
  c.dispose()
})

test('non-decision actions cannot submit formal approval or discussion', async () => {
  let writes = 0
  const c = createTaskController(client({ decide: async () => { writes++ }, post: async () => { writes++ } }))
  await c.select('room')
  await c.decide(approval, 'clarify')
  assert.equal(writes, 0)
  assert.equal(c.state.message, '')
  assert.equal(c.state.room.approvals[0].status, 'pending')
  c.dispose()
})

test('waiting on an approval decision does not block independent discussion', async () => {
  const pending = deferred(), decisions = [], messages = []
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
  assert.equal(decisions[0][0], 'room')
  assert.equal(decisions[0][1].payload_hash, approval.payload_hash)
  pending.resolve(); await vote; c.dispose()
})

test('switching rooms discards an old approval error and reloads the selected room', async () => {
  const pending = deferred()
  const c = createTaskController(client({ decide: () => pending.promise }))
  await c.select('old')
  const vote = c.decide(approval, 'rejected')
  await c.select('new')
  pending.reject(new Error('old room failure')); await vote
  assert.equal(c.state.room.id, 'new')
  assert.deepEqual(c.state.approvalErrors, {})
  c.dispose()
})

test('a selection during a pending refresh immediately schedules the new room read', async () => {
  const pending = deferred(), reads = []
  const c = createTaskController(client({ get: id => { reads.push(id); return id === 'old' ? pending.promise : Promise.resolve(room(id)) } }))
  const loading = c.select('old')
  await c.select('new')
  pending.resolve(room('old')); await loading
  assert.equal(c.state.room.id, 'new')
  assert.deepEqual(reads, ['old', 'new'])
  c.dispose()
})

test('ambiguous message delivery retains the request key on retry', async () => {
  const sent = []
  const c = createTaskController(client({ post: async (id, data) => {
    sent.push(data)
    if (sent.length === 1) throw new Error('lost response')
    return {}
  } }))
  await c.select('room'); c.state.message = '补充需求'
  await c.send(); await c.send()
  assert.equal(sent[0].client_request_id, sent[1].client_request_id)
  assert.equal(c.state.message, '')
  c.dispose()
})

test('approval transport binds the selected approval hash and snapshots the caller identity', async (t) => {
  const settings = { backend: 'python', endpoints: { python: 'http://api.test' }, accessToken: 'bob-token' }
  const calls = []
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push({ url, options }); return new Response(JSON.stringify({ status: 'approved' }))
  })
  const transport = createTaskClient(settings)
  settings.accessToken = 'alice-token'; settings.endpoints.python = 'http://other.test'
  await transport.decide('room/A', approval, 'approved')
  assert.equal(calls[0].url, 'http://api.test/tasks/room%2FA/approvals/approval-A')
  assert.equal(calls[0].options.headers.get('Authorization'), 'Bearer bob-token')
  assert.deepEqual(JSON.parse(calls[0].options.body), { decision: 'approved', payload_hash: approval.payload_hash })
})
