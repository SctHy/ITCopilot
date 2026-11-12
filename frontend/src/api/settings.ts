// Connection settings persisted in localStorage. The access token is never written here (see lib/auth.js).
import { storedToken } from '../lib/auth.ts'
import { DEFAULT_BACKENDS } from './http.ts'
import type { Settings } from '../lib/types.ts'

const SETTINGS_KEY = 'it-copilot.frontend.settings'

function readSettings(): Partial<Settings> {
  try {
    return JSON.parse(localStorage.getItem(SETTINGS_KEY) || '{}')
  } catch {
    return {}
  }
}

export function createInitialSettings(): Settings {
  const saved = readSettings()
  const savedPythonEndpoint = saved.endpoints?.python === '/api/python' ? 'http://127.0.0.1:8001' : saved.endpoints?.python
  return {
    // 身份由登录令牌决定（见 auth.js）；请求体不再携带 user_id / department。
    accessToken: typeof sessionStorage === 'undefined' ? '' : storedToken(),
    backend: 'python',
    conversationId: saved.conversationId || '',
    endpoints: {
      python: savedPythonEndpoint || DEFAULT_BACKENDS.python!.baseUrl
    }
  }
}

export function saveSettings(settings: Settings): void {
  localStorage.setItem(SETTINGS_KEY, JSON.stringify({ ...settings, accessToken: undefined }))
}
