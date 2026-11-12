import { LOGIN, machine } from './stateMachine.ts'
// 登录与令牌：dev 模式向后端申请演示身份令牌；oidc 模式走 Keycloak 授权码 + PKCE（公开客户端，不存 client secret）。
// 访问令牌只放 sessionStorage：关闭标签页即失效，不跨标签共享。身份展示用的 claims 仅供 UI，授权以后端校验为准。

const TOKEN_KEY = 'it-copilot.access-token'
const PKCE_KEY = 'it-copilot.pkce'

type TokenStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>

export interface JwtClaims {
  exp?: number
  sub?: string
  preferred_username?: string
  employee_id?: string
  department?: string
  roles?: string[]
  realm_access?: { roles?: string[] }
}

export interface OidcConfig { issuer?: string; client_id?: string }

function requireOidc(config: OidcConfig): { issuer: string; clientId: string } {
  // A misconfigured server would otherwise send the browser to "undefined/protocol/...".
  if (!config.issuer || !config.client_id) throw new Error('服务端 OIDC 配置缺少 issuer 或 client_id')
  return { issuer: config.issuer, clientId: config.client_id }
}

export function storedToken(storage: TokenStorage = sessionStorage): string {
  const token = storage.getItem(TOKEN_KEY) || ''
  const claims = parseJwt(token)
  // 提前 30 秒视为过期，避免请求途中失效。
  return tokenState(token, claims) === 'valid' ? token : ''
}

export function tokenState(token: string, claims: JwtClaims | null = parseJwt(token), now = Date.now()): 'missing' | 'invalid' | 'valid' | 'expired' {
  if (!token) return 'missing'
  if (!claims || typeof claims.exp !== 'number' || !Number.isFinite(claims.exp)) return 'invalid'
  return claims.exp * 1000 > now + 30000 ? 'valid' : 'expired'
}

export function storeToken(token: string, storage: TokenStorage = sessionStorage): void {
  if (token) storage.setItem(TOKEN_KEY, token)
  else storage.removeItem(TOKEN_KEY)
}

export function parseJwt(token: unknown): JwtClaims | null {
  const part = String(token || '').split('.')[1]
  if (!part) return null
  try {
    const json = atob(part.replace(/-/g, '+').replace(/_/g, '/').padEnd(Math.ceil(part.length / 4) * 4, '='))
    return JSON.parse(decodeURIComponent([...json].map(c => '%' + c.charCodeAt(0).toString(16).padStart(2, '0')).join('')))
  } catch {
    return null
  }
}

export function identityFromToken(token: string) {
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

function base64Url(bytes: ArrayBuffer | Uint8Array): string {
  return btoa(String.fromCharCode(...new Uint8Array(bytes))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

export async function pkcePair(cryptoImpl: Crypto = globalThis.crypto) {
  const verifier = base64Url(cryptoImpl.getRandomValues(new Uint8Array(32)))
  const challenge = base64Url(await cryptoImpl.subtle.digest('SHA-256', new TextEncoder().encode(verifier)))
  return { verifier, challenge }
}

export function authorizeUrl({ issuer, clientId, redirectUri, challenge, state }: { issuer: string; clientId: string; redirectUri: string; challenge: string; state: string }): string {
  const params = new URLSearchParams({
    response_type: 'code', client_id: clientId, redirect_uri: redirectUri, scope: 'openid',
    code_challenge: challenge, code_challenge_method: 'S256', state,
  })
  return `${issuer.replace(/\/+$/, '')}/protocol/openid-connect/auth?${params}`
}

function redirectUri() {
  return `${window.location.origin}${window.location.pathname}`
}

export async function startOidcLogin(config: OidcConfig): Promise<void> {
  const { issuer, clientId } = requireOidc(config)
  const { verifier, challenge } = await pkcePair()
  const state = base64Url(crypto.getRandomValues(new Uint8Array(16)))
  sessionStorage.setItem(PKCE_KEY, JSON.stringify({ verifier, state, issuer, clientId }))
  window.location.assign(authorizeUrl({ issuer, clientId, redirectUri: redirectUri(), challenge, state }))
}

// 回调页：校验 state，用 code + verifier 换取令牌，并从地址栏移除 code。
export async function completeOidcLogin(): Promise<string> {
  const params = new URLSearchParams(window.location.search)
  const code = params.get('code')
  if (!code) return ''
  const flow = machine(LOGIN, 'callback')
  try {
    const pending = JSON.parse(sessionStorage.getItem(PKCE_KEY) || 'null')
    sessionStorage.removeItem(PKCE_KEY)
    window.history.replaceState({}, '', redirectUri())
    if (!pending || pending.state !== params.get('state')) throw new Error('登录状态校验失败，请重新登录')
    flow.send('validate')
    const response = await fetch(`${pending.issuer.replace(/\/+$/, '')}/protocol/openid-connect/token`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ grant_type: 'authorization_code', code, redirect_uri: redirectUri(), client_id: pending.clientId, code_verifier: pending.verifier }),
    })
    if (!response.ok) throw new Error(`登录失败：${response.status}`)
    const token = (await response.json()).access_token
    if (typeof token !== 'string' || !token) throw new Error('登录响应缺少访问令牌')
    flow.send('token')
    storeToken(token)
    return token
  } catch (error) {
    if (flow.state !== 'authenticated') flow.send('fail')
    throw error
  }
}

export function oidcLogout(config: OidcConfig): void {
  storeToken('')
  const { issuer, clientId } = requireOidc(config)
  const params = new URLSearchParams({ client_id: clientId, post_logout_redirect_uri: redirectUri() })
  window.location.assign(`${issuer.replace(/\/+$/, '')}/protocol/openid-connect/logout?${params}`)
}
