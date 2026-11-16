import { test } from 'vitest'
import assert from 'node:assert/strict'
import { reactive, subscribe, version } from '../src/lib/store.ts'

test('writes anywhere in the tree notify the root, and nested reads keep their identity', () => {
  const state = reactive({ messages: [], room: null, busy: {} })
  let calls = 0
  const stop = subscribe(state, () => { calls++ })
  state.messages.push({ id: 'a', content: '' })
  const message = state.messages[0]
  assert.equal(state.messages.indexOf(message), 0, 'the same proxy is returned on every read')
  const before = version(state)
  message.content += '已'
  assert.ok(version(state) > before)
  state.busy.x = true
  delete state.busy.x
  state.room = { events: [{ id: 1 }] }
  state.room.events.splice(0, 1)
  assert.ok(calls >= 5)
  stop()
  const after = calls
  state.room = null
  assert.equal(calls, after, 'unsubscribed listeners are not called')
})

test('assigning a proxy stores the raw object and non-plain values pass through', () => {
  const state = reactive({ items: [], file: null })
  const item = reactive({ id: 'x' })
  state.items.push(item)
  assert.equal(state.items[0].id, 'x')
  const blob = new Blob(['a'])
  state.file = blob
  assert.equal(state.file, blob)
  assert.deepEqual(JSON.parse(JSON.stringify(state.items)), [{ id: 'x' }])
})

test('an unchanged write does not notify', () => {
  const state = reactive({ value: 1 })
  let calls = 0
  subscribe(state, () => { calls++ })
  state.value = 1
  assert.equal(calls, 0)
  state.value = 2
  assert.equal(calls, 1)
})
