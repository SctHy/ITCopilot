// Transport core: resolve the endpoint for a backend, attach the bearer token, parse JSON, map errors.
// Every API module goes through requestFor; nothing else talks to fetch for JSON calls.
import type { Settings } from '../lib/types.ts'

export interface BackendMeta { id: string; label: string; baseUrl: string; port: string }

/** Error for a non-2xx response; `status` lets callers tell "rejected" from "unreachable". */
export class ApiError extends Error {
  status: number
  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

interface RuntimeConfig { pythonApiUrl?: string }

function runtimeConfig(): RuntimeConfig {
  if (typeof window === 'undefined') return {}
  return (window as Window & { __IT_COPILOT_CONFIG__?: RuntimeConfig }).__IT_COPILOT_CONFIG__ || {}
}

export const DEFAULT_BACKENDS: Record<string, BackendMeta> = {
  python: {
    id: 'python',
    label: 'Python',
    baseUrl: runtimeConfig().pythonApiUrl || import.meta.env?.VITE_PYTHON_API_URL || 'http://127.0.0.1:8001',
    port: '8001'
  }
}

export function normalizeBaseUrl(value: unknown): string {
  return String(value || '').replace(/\/+$/, '')
}

export function backendMeta(type: string, settings: Pick<Settings, 'endpoints'>): BackendMeta {
  const meta = DEFAULT_BACKENDS[type] || DEFAULT_BACKENDS.python!
  return { ...meta, baseUrl: normalizeBaseUrl(settings.endpoints[type] || meta.baseUrl) }
}

export function authHeaders(settings: Pick<Settings, 'accessToken'>, extra: HeadersInit = {}): Headers {
  const headers = new Headers(extra)
  if (settings.accessToken) headers.set('Authorization', `Bearer ${settings.accessToken}`)
  return headers
}

export function jsonBody(payload: unknown): RequestInit {
  return { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) }
}

export async function requestFor<T = any>(type: string, settings: Settings, path: string, options: RequestInit = {}): Promise<T> {
  const headers = authHeaders(settings, options.headers || {})
  const url = `${backendMeta(type, settings).baseUrl}${path}`
  const response = await fetch(url, { ...options, headers,
    signal: options.signal || AbortSignal.timeout(path === '/chat' ? 120000 : 20000) })
  const text = await response.text()
  let data: unknown = null
  try {
    data = text ? JSON.parse(text) : null
  } catch {
    data = text
  }
  if (!response.ok) {
    const detail = typeof data === 'string' ? data : JSON.stringify(data)
    throw new ApiError(`${response.status} ${response.statusText}: ${detail}`, response.status)
  }
  return data as T
}
