import { test } from 'node:test'
import assert from 'node:assert/strict'
import { authorizeUrl, identityFromToken, parseJwt, pkcePair, storedToken } from '../src/lib/auth.js'
import { readEventStream } from '../src/lib/backends.js'

function jwt(claims) {
  const encode = (value) => Buffer.from(JSON.stringify(value)).toString('base64url')
  return `${encode({ alg: 'HS256' })}.${encode(claims)}.sig`
}

function memoryStorage(entries = {}) {
  const data = new Map(Object.entries(entries))
  return { getItem: k => data.get(k) ?? null, setItem: (k, v) => data.set(k, v), removeItem: k => data.delete(k) }
}

test('parses unicode claims and keeps only roles the API understands', () => {
  const token = jwt({ preferred_username: 'alice', department: '研发', employee_id: '1001', realm_access: { roles: ['employee', 'offline_access'] } })
  assert.equal(parseJwt(token).department, '研发')
  assert.deepEqual(identityFromToken(token), { username: 'alice', employeeId: '1001', department: '研发', roles: ['employee'] })
  assert.equal(parseJwt('not-a-token'), null)
})

test('expired or nearly expired tokens are not reused', () => {
  const now = Math.floor(Date.now() / 1000)
  assert.equal(storedToken(memoryStorage({ 'it-copilot.access-token': jwt({ exp: now + 10 }) })), '')
  const fresh = jwt({ exp: now + 3600 })
  assert.equal(storedToken(memoryStorage({ 'it-copilot.access-token': fresh })), fresh)
})

test('PKCE challenge is the S256 hash of the verifier and lands in the authorize URL', async () => {
  const { verifier, challenge } = await pkcePair()
  const expected = Buffer.from(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier))).toString('base64url')
  assert.equal(challenge, expected)
  const url = new URL(authorizeUrl({ issuer: 'http://kc/realms/it-copilot/', clientId: 'web', redirectUri: 'http://app/', challenge, state: 's1' }))
  assert.equal(url.pathname, '/realms/it-copilot/protocol/openid-connect/auth')
  assert.equal(url.searchParams.get('code_challenge_method'), 'S256')
  assert.equal(url.searchParams.get('code_challenge'), challenge)
})

test('SSE reader keeps event ids, skips heartbeats and handles split chunks', async () => {
  const text = 'id: 1-1\nevent: status\ndata: {"request_id":"r"}\n\n: heartbeat\n\nid: 1-2\nevent: tool_started\ndata: {"tool":"lookup"}\n\nid: 1-3\nevent: completed\ndata: {"ok":true}\n\n'
  const chunks = [text.slice(0, 25), text.slice(25, 90), text.slice(90)].map(part => new TextEncoder().encode(part))
  const body = new ReadableStream({ start(controller) { chunks.forEach(c => controller.enqueue(c)); controller.close() } })
  const events = []
  await readEventStream(body, (id, event, data) => events.push([id, event, data]))
  assert.deepEqual(events, [['1-1', 'status', { request_id: 'r' }], ['1-2', 'tool_started', { tool: 'lookup' }], ['1-3', 'completed', { ok: true }]])
})
