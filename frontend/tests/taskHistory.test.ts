import { deferred, requestKey, taskClient } from './fixtures.ts'
import type { TaskHistoryItem } from '../src/lib/taskHistoryController.ts'
import { test, vi } from 'vitest'
import assert from 'node:assert/strict'
import { createTaskHistoryController } from '../src/lib/taskHistoryController.ts'
import { createTaskClient } from '../src/lib/taskClient.ts'

function historyItem(id: string): TaskHistoryItem {
  return { id, title: id, status: 'open', steps_done: 0, steps_total: 0, pending_todos: 0, active_runs: 0 }
}

test('latest history search wins and a failed authorization refresh clears prior results', async () => {
  const pending = deferred<{ items: TaskHistoryItem[] }>(); let count = 0
  const c = createTaskHistoryController(taskClient({ history: async () => {
    count++
    if (count === 1) return pending.promise
    if (count === 2) return { items: [historyItem('new')] }
    throw new Error('access revoked')
  } }))
  const old = c.search(); c.state.query = 'new'; await c.search()
  pending.resolve({ items: [historyItem('old')] }); await old
  assert.equal(c.state.items[0]!.id, 'new')
  await c.search()
  assert.deepEqual(c.state.items, []); assert.equal(c.state.error, 'access revoked')
  c.dispose()
})

test('continued history uses the original task and retains its key after a lost response', async () => {
  const calls: { id: string; payload: unknown }[] = [], opened: string[] = []
  const c = createTaskHistoryController(taskClient({ continue: async (id, payload) => {
    calls.push({ id, payload })
    if (calls.length === 1) throw new Error('response lost')
    return { task_id: id }
  } }), 'current-room', id => opened.push(id))
  await c.resume(historyItem('old-room'), 'continue')
  await c.resume(historyItem('old-room'), 'continue')
  assert.deepEqual(calls.map(x => x.id), ['old-room', 'old-room'])
  assert.equal(requestKey(calls[0]!.payload), requestKey(calls[1]!.payload))
  assert.deepEqual(opened, ['old-room']); c.dispose()
})

test('identity switch discards pending history and continuation navigation', async () => {
  const search = deferred<{ items: TaskHistoryItem[] }>(), resume = deferred<{ task_id: string }>(), opened: string[] = []
  const c = createTaskHistoryController(taskClient({ history: () => search.promise, continue: () => resume.promise }), '', id => opened.push(id))
  const reading = c.search(), continuing = c.resume(historyItem('old'), 'continue')
  c.dispose()
  search.resolve({ items: [historyItem('private')] }); resume.resolve({ task_id: 'old' })
  await Promise.all([reading, continuing])
  assert.deepEqual(c.state.items, []); assert.deepEqual(opened, [])
})

test('links refresh through the current viewer access and do not copy source content', async () => {
  const calls: unknown[][] = []
  const c = createTaskHistoryController(taskClient({
    capability: async (...args) => calls.push(args),
    history: async params => { assert.equal(params.exclude_id, 'room'); return { items: [] } },
    linked: async id => { assert.equal(id, 'room'); return { items: [] } }
  }), 'room')
  await c.link('source', true)
  assert.deepEqual(calls, [['room', 'links', { target_id: 'source', linked: true }]])
  assert.deepEqual(c.state.links, []); c.dispose()
})

test('history transport snapshots the endpoint and token for search, links and continuation', async () => {
  const settings = { backend: 'python', endpoints: { python: 'http://original.test' }, accessToken: 'alice', conversationId: '' }, calls: Request[] = []
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, options) => { calls.push(new Request(url, options)); return new Response(JSON.stringify({ items: [] })) })
  const client = createTaskClient(settings)
  settings.accessToken = 'bob'; settings.endpoints.python = 'http://changed.test'
  await client.history({ q: 'VPN', limit: 8 }); await client.linked('room/A')
  await client.continue('source/A', { message: 'continue', client_request_id: 'key' })
  assert.equal(calls[0]!.url, 'http://original.test/task-history?q=VPN&limit=8')
  assert.equal(calls[1]!.url, 'http://original.test/tasks/room%2FA/links')
  assert.equal(calls[2]!.url, 'http://original.test/task-history/source%2FA/continue')
  assert.ok(calls.every(c => c.headers.get('Authorization') === 'Bearer alice'))
})
