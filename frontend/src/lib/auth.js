// 登录与令牌：dev 模式向后端申请演示身份令牌；oidc 模式走 Keycloak 授权码 + PKCE（公开客户端，不存 client secret）。
// 访问令牌只放 sessionStorage：关闭标签页即失效，不跨标签共享。身份展示用的 claims 仅供 UI，授权以后端校验为准。

const TOKEN_KEY = 'it-copilot.access-token'
const PKCE_KEY = 'it-copilot.pkce'

export function storedToken(storage = sessionStorage) {
  const token = storage.getItem(TOKEN_KEY) || ''
  const claims = parseJwt(token)
  // 提前 30 秒视为过期，避免请求途中失效。
  return claims && claims.exp * 1000 > Date.now() + 30000 ? token : ''
}

export function storeToken(token, storage = sessionStorage) {
  if (token) storage.setItem(TOKEN_KEY, token)
  else storage.removeItem(TOKEN_KEY)
}

export function parseJwt(token) {
  const part = String(token || '').split('.')[1]
  if (!part) return null
  try {
    const json = atob(part.replace(/-/g, '+').replace(/_/g, '/').padEnd(Math.ceil(part.length / 4) * 4, '='))
    return JSON.parse(decodeURIComponent([...json].map(c => '%' + c.charCodeAt(0).toString(16).padStart(2, '0')).join('')))
  } catch {
    return null
  }
}

export function identityFromToken(token) {
  const claims = parseJwt(token)
  if (!claims) return null
  const roles = claims.roles || claims.realm_access?.roles || []
  return {
    username: claims.preferred_username || claims.sub,
    employeeId: claims.employee_id || '',
    department: claims.department || '',
    roles: roles.filter(role => ['employee', 'approver', 'it_admin', 'auditor'].includes(role)),
  }
}

function base64Url(bytes) {
  return btoa(String.fromCharCode(...new Uint8Array(bytes))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

export async function pkcePair(cryptoImpl = globalThis.crypto) {
  const verifier = base64Url(cryptoImpl.getRandomValues(new Uint8Array(32)))
  const challenge = base64Url(await cryptoImpl.subtle.digest('SHA-256', new TextEncoder().encode(verifier)))
  return { verifier, challenge }
}

export function authorizeUrl({ issuer, clientId, redirectUri, challenge, state }) {
  const params = new URLSearchParams({
    response_type: 'code', client_id: clientId, redirect_uri: redirectUri, scope: 'openid',
    code_challenge: challenge, code_challenge_method: 'S256', state,
  })
  return `${issuer.replace(/\/+$/, '')}/protocol/openid-connect/auth?${params}`
}

function redirectUri() {
  return `${window.location.origin}${window.location.pathname}`
}

export async function startOidcLogin(config) {
  const { verifier, challenge } = await pkcePair()
  const state = base64Url(crypto.getRandomValues(new Uint8Array(16)))
  sessionStorage.setItem(PKCE_KEY, JSON.stringify({ verifier, state, issuer: config.issuer, clientId: config.client_id }))
  window.location.assign(authorizeUrl({ issuer: config.issuer, clientId: config.client_id, redirectUri: redirectUri(), challenge, state }))
}

// 回调页：校验 state，用 code + verifier 换取令牌，并从地址栏移除 code。
export async function completeOidcLogin() {
  const params = new URLSearchParams(window.location.search)
  const code = params.get('code')
  if (!code) return ''
  const pending = JSON.parse(sessionStorage.getItem(PKCE_KEY) || 'null')
  sessionStorage.removeItem(PKCE_KEY)
  window.history.replaceState({}, '', redirectUri())
  if (!pending || pending.state !== params.get('state')) throw new Error('登录状态校验失败，请重新登录')
  const response = await fetch(`${pending.issuer.replace(/\/+$/, '')}/protocol/openid-connect/token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ grant_type: 'authorization_code', code, redirect_uri: redirectUri(), client_id: pending.clientId, code_verifier: pending.verifier }),
  })
  if (!response.ok) throw new Error(`登录失败：${response.status}`)
  const token = (await response.json()).access_token
  storeToken(token)
  return token
}

export function oidcLogout(config) {
  storeToken('')
  const params = new URLSearchParams({ client_id: config.client_id, post_logout_redirect_uri: redirectUri() })
  window.location.assign(`${config.issuer.replace(/\/+$/, '')}/protocol/openid-connect/logout?${params}`)
}
