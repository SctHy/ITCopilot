import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createTaskDetailsController } from '../src/lib/taskDetailsController.js'
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
