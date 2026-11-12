import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createAuthSession } from '../src/lib/authSession.ts'
import { createRequestState } from '../src/lib/requestState.ts'

const httpError = (status) => Object.assign(new Error(`${status}`), { status })
const alice = { subject: 'alice', username: 'alice', department: 'IT', roles: ['employee'] }

function session(overrides = {}, accessToken = 'saved-token') {
  const settings = { backend: 'python', endpoints: { python: 'http://api.test' }, conversationId: '', accessToken }
  const stored = [], toasts = []
  let switches = 0
  const transport = { config: async () => ({ mode: 'oidc' }), me: async () => alice, devToken: async (t, s, user) => ({ access_token: `${user}-token` }),
    completeOidcLogin: async () => '', startOidcLogin() {}, oidcLogout() {}, storeToken: token => stored.push(token), ...overrides }
  const auth = createAuthSession(settings, createRequestState(settings), transport,
    { toast: message => toasts.push(message), onIdentityChange: () => { switches++ } })
  return { auth, settings, stored, toasts, switches: () => switches }
}

test('a network failure while checking identity keeps the token', async () => {
  const { auth, settings, stored, toasts } = session({ me: async () => { throw new Error('fetch failed') } })
  await auth.init()
  assert.equal(auth.state.identity, null)
  assert.equal(settings.accessToken, 'saved-token')
  assert.deepEqual(stored, [])
  assert.match(toasts[0], /身份校验失败/)
})

test('a token the server rejects is discarded', async () => {
  const { auth, settings, stored } = session({ me: async () => { throw httpError(401) } })
  await auth.init()
  assert.equal(settings.accessToken, '')
  assert.deepEqual(stored, [''])
})

test('a failed OIDC callback still loads the login options', async () => {
  const { auth, toasts } = session({ completeOidcLogin: async () => { throw new Error('登录状态校验失败') } }, '')
  await auth.init()
  assert.equal(auth.state.config.mode, 'oidc')
  assert.match(toasts[0], /登录失败/)
})

test('dev mode signs in as alice when there is no token, and a switch resets the conversation', async () => {
  const { auth, settings, switches } = session({ config: async () => ({ mode: 'dev', users: [] }) }, '')
  await auth.init()
  assert.equal(settings.accessToken, 'alice-token')
  assert.equal(auth.state.identity.username, 'alice')
  assert.equal(switches(), 1)
})

test('an OIDC redirect that cannot start is reported instead of failing silently', async () => {
  const { auth, toasts } = session({ startOidcLogin: async () => { throw new Error('服务端 OIDC 配置缺少 issuer 或 client_id') } }, '')
  await auth.init()
  auth.loginOidc()
  await new Promise(resolve => setTimeout(resolve))
  assert.match(toasts.at(-1), /缺少 issuer/)
})
