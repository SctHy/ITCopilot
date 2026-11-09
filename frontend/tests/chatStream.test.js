import { test } from 'node:test'
import assert from 'node:assert/strict'
import { requestChatStream } from '../src/lib/backends.js'

const settings = { backend: 'python', endpoints: { python: 'http://api.test' }, conversationId: 'c1', accessToken: '' }

function sse(...events) {
  const text = events.map(([id, event, data]) => `id: ${id}\nevent: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join('')
  return new Response(new ReadableStream({ start(c) { c.enqueue(new TextEncoder().encode(text)); c.close() } }), { status: 200 })
}

function withFetch(responses, fn) {
  const calls = []
  const original = globalThis.fetch
  globalThis.fetch = async (url, init) => { calls.push({ url, init }); return responses.shift() }
  return fn(calls).finally(() => { globalThis.fetch = original })
}

test('resumes with Last-Event-ID after the stream ends without a terminal event', () =>
  withFetch([
    sse(['1-1', 'status', { request_id: 'r1' }]),
    sse(['1-2', 'completed', { request_id: 'r1', response: '好了' }])
  ], async (calls) => {
    const result = await requestChatStream('python', settings, '你好')
    assert.equal(result.requestId, 'r1')
    assert.match(calls[1].url, /\/chat\/stream\/r1$/)
    assert.equal(calls[1].init.headers.get('Last-Event-ID'), '1-1')
  }))

test('an abandoned run is reported as interrupted and a resend starts a new request', () =>
  withFetch([
    sse(['1-1', 'status', { request_id: 'r2' }], ['1-2', 'error', { status_code: 504, detail: 'stream_abandoned' }]),
    sse(['2-1', 'completed', { request_id: 'r3', response: 'ok' }])
  ], async (calls) => {
    await assert.rejects(requestChatStream('python', settings, '再来'), /服务端处理中断/)
    await requestChatStream('python', settings, '再来')
    const ids = calls.map(c => JSON.parse(c.init.body).client_request_id)
    assert.notEqual(ids[0], ids[1])
  }))
