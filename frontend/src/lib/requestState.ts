// 对后端、设置和请求代次做快照，过滤切换或重复请求后的旧结果。
import { requestPhase, type RequestPhase } from './stateMachine.ts'
import type { Settings } from './types.ts'

export interface TrackedRequest {
  readonly backend: string
  readonly settings: Settings
  readonly phase: RequestPhase
  isCurrent(): boolean
}

export interface RequestState {
  invalidate(): void
  begin(slot: string): TrackedRequest
}

export function createRequestState(settings: Settings): RequestState {
  let generation = 0
  let sequence = 0
  const latest = new Map<string, number>()
  return {
    invalidate() { generation += 1 },
    begin(slot) {
      const revision = generation
      const id = ++sequence
      const backend = settings.backend
      const snapshot: Settings = { ...settings, endpoints: { ...settings.endpoints } }
      latest.set(slot, id)
      const current = () => revision === generation && latest.get(slot) === id &&
        snapshot.accessToken === settings.accessToken &&
        backend === settings.backend && snapshot.endpoints[backend] === settings.endpoints[backend]
      return {
        backend,
        settings: snapshot,
        get phase() { return requestPhase(current()) },
        isCurrent: () => requestPhase(current()) === 'current'
      }
    }
  }
}

/**
 * Run one request in `slot` and apply only a still-current outcome, so a response that
 * arrives after a connection, identity or newer request change cannot overwrite state.
 */
export async function loadCurrent<T>(
  requestState: RequestState,
  slot: string,
  fetch: (backend: string, settings: Settings) => Promise<T>,
  apply: (value: T, request: TrackedRequest) => void,
  fail?: (error: Error) => void
): Promise<void> {
  const request = requestState.begin(slot)
  try {
    const value = await fetch(request.backend, request.settings)
    if (request.isCurrent()) apply(value, request)
  } catch (error) {
    if (request.isCurrent()) fail?.(error instanceof Error ? error : new Error(String(error)))
  }
}
