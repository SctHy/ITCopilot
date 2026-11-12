// Who is signed in, and how they sign in. Transport is injected; the server stays the authority on roles.
import { reactive } from 'vue'
import { loadCurrent, type RequestState } from './requestState.ts'
import type { Identity, Role, Settings } from './types.ts'

export interface AuthConfig {
  mode: 'dev' | 'oidc' | (string & {})
  users?: { username: string; department: string; roles: Role[] }[]
  issuer?: string
  client_id?: string
}

export interface AuthTransport {
  config(type: string, settings: Settings): Promise<AuthConfig>
  me(type: string, settings: Settings): Promise<Identity>
  devToken(type: string, settings: Settings, username: string): Promise<{ access_token: string }>
  /** Finishes an OIDC redirect if the URL carries one; resolves '' otherwise. */
  completeOidcLogin(): Promise<string>
  startOidcLogin(config: AuthConfig): unknown
  oidcLogout(config: AuthConfig): unknown
  storeToken(token: string): void
}

export interface AuthSession {
  state: { identity: Identity | null; config: AuthConfig | null; devUser: string }
  init(): Promise<void>
  loadIdentity(): Promise<void>
  loginAs(username: string, options?: { quiet?: boolean }): Promise<void>
  loginOidc(): void
  logoutOidc(): void
}

export function createAuthSession(settings: Settings, requestState: RequestState, transport: AuthTransport,
  hooks: { toast(message: string): void; onIdentityChange(): void }): AuthSession {
  const state = reactive<AuthSession['state']>({ identity: null, config: null, devUser: 'alice' })
  const errorMessage = (error: unknown) => error instanceof Error ? error.message : String(error)

  // 身份初始化：先处理 OIDC 回调；dev 模式无令牌时默认以 alice 登录，便于本地演示。
  // 回调失败（state 不符、换取令牌失败）也要继续加载登录配置，否则连接配置里没有登录按钮。
  async function init() {
    try {
      const callbackToken = await transport.completeOidcLogin()
      if (callbackToken) settings.accessToken = callbackToken
    } catch (error) {
      hooks.toast(`登录失败：${errorMessage(error)}`)
    }
    try {
      state.config = await transport.config(settings.backend, settings)
      if (!settings.accessToken && state.config?.mode === 'dev') await loginAs(state.identity?.username || 'alice', { quiet: true })
      await loadIdentity()
    } catch (error) {
      hooks.toast(`身份初始化失败：${errorMessage(error)}`)
    }
  }

  async function loadIdentity() {
    if (!settings.accessToken) { state.identity = null; return }
    await loadCurrent(requestState, 'identity', transport.me,
      loaded => { state.identity = loaded; state.devUser = loaded.username },
      error => {
        state.identity = null
        // 只有服务端拒绝令牌才丢弃它；网络抖动或服务暂不可用时保留，恢复后重新检查即可。
        const status = (error as Error & { status?: number }).status
        if (status === 401 || status === 403) { settings.accessToken = ''; transport.storeToken('') }
        else hooks.toast(`身份校验失败：${error.message}`)
      })
  }

  async function loginAs(username: string, { quiet = false } = {}) {
    const request = requestState.begin('login')
    const { access_token: token } = await transport.devToken(request.backend, request.settings, username)
    if (!request.isCurrent()) return
    settings.accessToken = token
    transport.storeToken(token)
    // 换身份即换会话：避免把上一个人的对话与审批卡片展示给新身份。
    hooks.onIdentityChange()
    await loadIdentity()
    if (!quiet) hooks.toast(`已切换为 ${username}`)
  }

  async function redirect(action: (config: AuthConfig) => unknown) {
    if (!state.config) return
    try { await action(state.config) } catch (error) { hooks.toast(errorMessage(error)) }
  }

  return {
    state, init, loadIdentity, loginAs,
    loginOidc: () => { redirect(transport.startOidcLogin) },
    logoutOidc: () => { redirect(transport.oidcLogout) }
  }
}
