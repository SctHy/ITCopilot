export const viewPaths = { chat: '/chat', knowledge: '/knowledge', approvals: '/approvals', evaluation: '/operations' } as const
export type ViewId = keyof typeof viewPaths
export interface AppSearch { code?: string; state?: string; session_state?: string; iss?: string; error?: string; error_description?: string }

export function validateAppSearch(search: Record<string, unknown>): AppSearch {
  const result: AppSearch = {}
  for (const key of ['code', 'state', 'session_state', 'iss', 'error', 'error_description'] as const) {
    if (typeof search[key] === 'string' && search[key]) result[key] = search[key]
  }
  return result
}

export function viewForPath(pathname: string): ViewId {
  return (Object.entries(viewPaths).find(([, path]) => path === pathname.replace(/\/$/, ''))?.[0] as ViewId) || 'chat'
}
