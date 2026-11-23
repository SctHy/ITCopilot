import { test, vi } from 'vitest'
import assert from 'node:assert/strict'
import { authorizeUrl, startOidcLogin, completeOidcLogin, identityFromToken, parseJwt, pkcePair, storedToken } from '../src/lib/auth.ts'
import { readEventStream } from '../src/api/chat.ts'

function jwt(claims: Record<string, unknown>) {
  const encode = (value: unknown) => Buffer.from(JSON.stringify(value)).toString('base64url')
  return `${encode({ alg: 'HS256' })}.${encode(claims)}.sig`
}

function memoryStorage(entries: Record<string, string> = {}): Pick<Storage, 'getItem' | 'setItem' | 'removeItem'> {
  const data = new Map(Object.entries(entries))
  return { getItem: k => data.get(k) ?? null, setItem: (k, v) => data.set(k, v), removeItem: k => data.delete(k) }
}

test('parses unicode claims and keeps only roles the API understands', () => {
  const token = jwt({ preferred_username: 'alice', department: '研发', employee_id: '1001', realm_access: { roles: ['employee', 'offline_access'] } })
  assert.equal(parseJwt(token)?.department, '研发')
  assert.deepEqual(identityFromToken(token), { username: 'alice', employeeId: '1001', department: '研发', roles: ['employee'] })
  assert.equal(parseJwt('not-a-token'), null)
})

test('expired or nearly expired tokens are not reused', () => {
  const now = Math.floor(Date.now() / 1000)
  assert.equal(storedToken(memoryStorage({ 'servicedesk-agent.access-token': jwt({ exp: now + 10 }) })), '')
  const fresh = jwt({ exp: now + 3600 })
  assert.equal(storedToken(memoryStorage({ 'servicedesk-agent.access-token': fresh })), fresh)
})

test('PKCE challenge is the S256 hash of the verifier and lands in the authorize URL', async () => {
  const { verifier, challenge } = await pkcePair()
  const expected = Buffer.from(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier))).toString('base64url')
  assert.equal(challenge, expected)
  const url = new URL(authorizeUrl({ issuer: 'http://kc/realms/servicedesk-agent/', clientId: 'web', redirectUri: 'http://app/', challenge, state: 's1' }))
  assert.equal(url.pathname, '/realms/servicedesk-agent/protocol/openid-connect/auth')
  assert.equal(url.searchParams.get('code_challenge_method'), 'S256')
  assert.equal(url.searchParams.get('code_challenge'), challenge)
})

test('SSE reader keeps event ids, skips heartbeats and handles split chunks', async () => {
  const text = 'id: 1-1\nevent: status\ndata: {"request_id":"r"}\n\n: heartbeat\n\nid: 1-2\nevent: tool_started\ndata: {"tool":"lookup"}\n\nid: 1-3\nevent: completed\ndata: {"ok":true}\n\n'
  const chunks = [text.slice(0, 25), text.slice(25, 90), text.slice(90)].map(part => new TextEncoder().encode(part))
  const body = new ReadableStream({ start(controller) { chunks.forEach(c => controller.enqueue(c)); controller.close() } })
  const events: unknown[][] = []
  await readEventStream(body, (id, event, data) => { events.push([id, event, data]) })
  assert.deepEqual(events, [['1-1', 'status', { request_id: 'r' }], ['1-2', 'tool_started', { tool: 'lookup' }], ['1-3', 'completed', { ok: true }]])
})

function oidcEnvironment(href: string) {
  let location = new URL(href)
  let assigned = ''
  const browser = {
    get location() { return { origin: location.origin, pathname: location.pathname, search: location.search, hash: location.hash, assign: (url: string) => { assigned = url } } },
    history: { state: { __TSR_index: 3 }, replaceState(_state: unknown, _title: string, url: string) { location = new URL(url, location) } },
  }
  vi.stubGlobal('window', browser)
  vi.stubGlobal('sessionStorage', memoryStorage())
  return { get location() { return location }, get assigned() { return assigned }, go: (href: string) => { location = new URL(href) } }
}

test('OIDC returns to the shared task while exchanging the original redirect URI', async () => {
  const env = oidcEnvironment('http://app/tasks?task=room-42')
  await startOidcLogin({ issuer: 'http://kc/realms/it', client_id: 'web' })
  const authorize = new URL(env.assigned)
  env.go(`http://app/tasks?code=code-1&state=${authorize.searchParams.get('state')}`)
  let exchanged: URLSearchParams | undefined
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (_url, options) => {
    assert.ok(options?.body instanceof URLSearchParams)
    exchanged = options.body
    return new Response(JSON.stringify({ access_token: 'token-1' }))
  })
  assert.equal(await completeOidcLogin(), 'token-1')
  assert.equal(exchanged?.get('redirect_uri'), authorize.searchParams.get('redirect_uri'))
  assert.equal(env.location.href, 'http://app/tasks?task=room-42')
})

test('OIDC rejects a mismatched state without exchanging or storing a token', async () => {
  const env = oidcEnvironment('http://app/tasks?code=c&state=wrong')
  sessionStorage.setItem('servicedesk-agent.pkce', JSON.stringify({ state: 'expected', verifier: 'v', issuer: 'http://kc', clientId: 'web' }))
  const fetch = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => { throw new Error('must not exchange') })
  await assert.rejects(completeOidcLogin(), /登录状态校验失败/)
  assert.equal(fetch.mock.calls.length, 0)
  assert.equal(sessionStorage.getItem('servicedesk-agent.access-token'), null)
  assert.equal(env.location.search, '')
})

test('OIDC reports authorization denial and removes callback parameters', async () => {
  const env = oidcEnvironment('http://app/tasks?error=access_denied&error_description=Denied&state=s')
  sessionStorage.setItem('servicedesk-agent.pkce', JSON.stringify({ state: 's', verifier: 'v', issuer: 'http://kc', clientId: 'web' }))
  const fetch = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => { throw new Error('must not exchange') })
  await assert.rejects(completeOidcLogin(), /拒绝登录：Denied/)
  assert.equal(fetch.mock.calls.length, 0)
  assert.equal(env.location.search, '')
  assert.equal(sessionStorage.getItem('servicedesk-agent.pkce'), null)
})

test('OIDC does not follow an external saved return destination', async () => {
  const env = oidcEnvironment('http://app/tasks?code=c&state=s')
  sessionStorage.setItem('servicedesk-agent.pkce', JSON.stringify({ state: 's', verifier: 'v', issuer: 'http://kc', clientId: 'web', returnTo: 'https://elsewhere.example/private' }))
  vi.spyOn(globalThis, 'fetch').mockImplementation(async () => new Response(JSON.stringify({ access_token: 'token-1' })))
  assert.equal(await completeOidcLogin(), 'token-1')
  assert.equal(env.location.href, 'http://app/tasks')
})
