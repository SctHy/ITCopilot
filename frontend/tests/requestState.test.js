import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createRequestState, loadCurrent } from '../src/lib/requestState.ts'

function state() { return { backend:'python', conversationId:'', endpoints:{python:'/py'} } }

test('changing the endpoint and back invalidates old responses', () => {
  const settings = state()
  const requests = createRequestState(settings)
  const old = requests.begin('stats')
  settings.endpoints.python = '/other'; requests.invalidate()
  settings.endpoints.python = '/py'; requests.invalidate()
  assert.equal(old.isCurrent(), false)
  assert.equal(requests.begin('stats').isCurrent(), true)
})

test('latest request wins within each slot and request settings are snapshots', () => {
  const settings = state()
  const requests = createRequestState(settings)
  const old = requests.begin('stats')
  const fresh = requests.begin('stats')
  requests.begin('monitor')
  assert.equal(old.isCurrent(), false)
  assert.equal(fresh.isCurrent(), true)
  settings.endpoints.python = '/changed'
  assert.equal(fresh.settings.endpoints.python, '/py')
  assert.equal(fresh.isCurrent(), false)
})

// Every loader (stats, monitor, skills, identity, approvals…) goes through loadCurrent.
for (const fail of [false, true]) {
  test(`loadCurrent: stale ${fail ? 'error' : 'success'} cannot overwrite switched state`, async () => {
    const settings = state()
    const requests = createRequestState(settings)
    let resolve, reject
    const pending = new Promise((a, b) => { resolve = a; reject = b })
    const field = { value: 'new' }
    const task = loadCurrent(requests, 'stats', () => pending, value => { field.value = value }, () => { field.value = 'failed' })
    settings.endpoints.python = '/other'; requests.invalidate()
    if (fail) reject(new Error('old')); else resolve('old')
    await task
    assert.equal(field.value, 'new')
  })
}

test('loadCurrent applies a current result with the request snapshot, and reports current errors', async () => {
  const settings = state()
  const requests = createRequestState(settings)
  const seen = []
  await loadCurrent(requests, 'approvals', (backend, snapshot) => Promise.resolve([backend, snapshot.endpoints.python]),
    (value, request) => seen.push(value, request.backend))
  await loadCurrent(requests, 'approvals', () => Promise.reject(new Error('down')), () => seen.push('applied'), error => seen.push(error.message))
  assert.deepEqual(seen, [['python', '/py'], 'python', 'down'])
})

test('changing identity invalidates responses even when the endpoint stays the same', () => {
  const settings = { ...state(), accessToken: 'alice' }
  const requests = createRequestState(settings)
  const pending = requests.begin('approvals')
  settings.accessToken = 'bob'
  assert.equal(pending.isCurrent(), false)
})

test('approval inbox discards a response from the previous identity', async () => {
  const settings = { ...state(), accessToken: 'alice' }
  const requestState = createRequestState(settings)
  let resolve
  const pending = new Promise(r => { resolve = r })
  const inbox = { value: [] }
  const task = loadCurrent(requestState, 'approvals', () => pending, body => { inbox.value = body.items })
  settings.accessToken = 'bob'
  requestState.invalidate()
  resolve({ items: [{ approval_id: 'alice-private-approval' }] })
  await task
  assert.deepEqual(inbox.value, [])
})
