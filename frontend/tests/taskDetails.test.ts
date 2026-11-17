import { deferred, requestKey, taskClient, workspace } from './fixtures.ts'
import type { TaskWorkspaceData } from '../src/lib/types.ts'
import { test } from 'vitest'
import assert from 'node:assert/strict'
import { createTaskDetailsController } from '../src/lib/taskDetailsController.ts'

test('a lost todo response retains its idempotency key; unrelated actions stay independent', async () => {
  const calls: { action: string; payload: unknown }[] = [], pending = deferred()
  const c = createTaskDetailsController(taskClient({ workspace: async () => workspace(''), capability: async (_id, action, payload) => {
    calls.push({ action, payload })
    if (action === 'progress') return pending.promise
    if (calls.filter(c => c.action === 'todos').length === 1) throw new Error('response lost')
  } }), 'room')
  const progress = c.perform('progress', {version: 0})
  assert.equal(await c.perform('todos', { question: 'error?' }), false)
  assert.equal(await c.perform('todos', { question: 'error?' }), true)
  assert.equal(requestKey(calls[1]!.payload), requestKey(calls[2]!.payload))
  pending.resolve(); await progress
  c.dispose()
})

test('disposed workspace cannot receive old errors or data', async () => {
  const pending = deferred<TaskWorkspaceData>(), c = createTaskDetailsController(taskClient({ workspace: () => pending.promise }), 'old')
  const request = c.refresh(); c.dispose(); pending.resolve(workspace('old'))
  await request; assert.equal(c.state.data, null)
})

test('latest workspace refresh wins', async () => {
  const pending = deferred<TaskWorkspaceData>(); let count = 0
  const c = createTaskDetailsController(taskClient({ workspace: () => ++count === 1 ? pending.promise : Promise.resolve(workspace('new')) }), 'room')
  const first = c.refresh(); await c.refresh(); pending.resolve(workspace('old')); await first
  assert.equal(c.state.data?.progress.goal, 'new'); c.dispose()
})

test('a backend slower than the poll still delivers data, without piling up reads', async () => {
  const pending = deferred<TaskWorkspaceData>(); let calls = 0
  const c = createTaskDetailsController(taskClient({ workspace: () => ++calls === 1 ? pending.promise : Promise.resolve(workspace('second')) }), 'room')
  const first = c.refresh()
  c.refresh(); c.refresh() // poll ticks while the first read is still in flight
  pending.resolve(workspace('first')); await first
  assert.equal(c.state.data?.progress.goal, 'second')
  assert.equal(calls, 2)
  c.dispose()
})

test('a failed refresh error clears once a later refresh succeeds', async () => {
  let fail = true
  const c = createTaskDetailsController(taskClient({ workspace: async () => { if (fail) throw new Error('blip'); return workspace('ok') } }), 'room')
  await c.refresh(); assert.equal(c.state.error, 'blip')
  fail = false; await c.refresh()
  assert.equal(c.state.error, ''); assert.equal(c.state.data?.progress.goal, 'ok')
  c.dispose()
})
