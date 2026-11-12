import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createTaskDetailsController } from '../src/lib/taskDetailsController.ts'
const deferred = () => { let resolve, reject; const promise = new Promise((a,b) => { resolve=a; reject=b }); return { resolve, reject, promise } }

test('a lost todo response retains its idempotency key; unrelated actions stay independent', async () => {
  const calls = [], pending = deferred()
  const c = createTaskDetailsController({ workspace: async () => ({}), capability: async (id, action, payload) => {
    calls.push({id, action, payload})
    if (action === 'progress') return pending.promise
    if (calls.filter(c => c.action === 'todos').length === 1) throw new Error('response lost')
  } }, 'room')
  const progress = c.perform('progress', {version: 0})
  assert.equal(await c.perform('todos', { question: 'error?' }), false)
  assert.equal(await c.perform('todos', { question: 'error?' }), true)
  assert.equal(calls[1].payload.client_request_id, calls[2].payload.client_request_id)
  pending.resolve(); await progress
  c.dispose()
})

test('disposed workspace cannot receive old errors or data', async () => {
  const pending = deferred(), c = createTaskDetailsController({ workspace: () => pending.promise }, 'old')
  const request = c.refresh(); c.dispose(); pending.resolve({progress: 'old'})
  await request; assert.equal(c.state.data, null)
})

test('latest workspace refresh wins', async () => {
  const pending = deferred(); let count = 0
  const c = createTaskDetailsController({ workspace: () => ++count === 1 ? pending.promise : Promise.resolve({progress: 'new'}) }, 'room')
  const first = c.refresh(); await c.refresh(); pending.resolve({progress: 'old'}); await first
  assert.equal(c.state.data.progress, 'new'); c.dispose()
})

test('a backend slower than the poll still delivers data, without piling up reads', async () => {
  const pending = deferred(); let calls = 0
  const c = createTaskDetailsController({ workspace: () => ++calls === 1 ? pending.promise : Promise.resolve({ progress: 'second' }) }, 'room')
  const first = c.refresh()
  c.refresh(); c.refresh() // poll ticks while the first read is still in flight
  pending.resolve({ progress: 'first' }); await first
  assert.equal(c.state.data.progress, 'second')
  assert.equal(calls, 2)
  c.dispose()
})

test('a failed refresh error clears once a later refresh succeeds', async () => {
  let fail = true
  const c = createTaskDetailsController({ workspace: async () => { if (fail) throw new Error('blip'); return { progress: 'ok' } } }, 'room')
  await c.refresh(); assert.equal(c.state.error, 'blip')
  fail = false; await c.refresh()
  assert.equal(c.state.error, ''); assert.equal(c.state.data.progress, 'ok')
  c.dispose()
})
