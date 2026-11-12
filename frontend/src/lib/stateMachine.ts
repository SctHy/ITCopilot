// Event tables for transient UI protocols. Durable data stays in its original store.
export type TransitionTable = Record<string, Record<string, string>>

export function transition(table: TransitionTable, state: string, event: string): string {
  const next = table[state]?.[event]
  if (!next) throw new Error(`Illegal transition: ${state} + ${event}`)
  return next
}

export interface Machine {
  readonly state: string
  send(event: string): string
}

export function machine(table: TransitionTable, initial: string): Machine {
  let state = initial
  return { get state() { return state }, send(event) { state = transition(table, state, event); return state } }
}

export const STREAM = {
  opening: { connected: 'reading', interrupt: 'interrupted', fail: 'failed' },
  reading: { resume: 'resuming', complete: 'completed', fail: 'failed', interrupt: 'interrupted' },
  resuming: { connected: 'reading', interrupt: 'interrupted', fail: 'failed' }
} satisfies TransitionTable
export const DRAFT = {
  absent: { delta: 'writing', reset: 'absent', remove: 'removed' },
  writing: { delta: 'writing', reset: 'empty', remove: 'removed' },
  empty: { delta: 'writing', reset: 'empty', remove: 'removed' },
  removed: { remove: 'removed' }
} satisfies TransitionTable
export const ACTIVITY = {
  idle: { start: 'busy', settle: 'idle' },
  busy: { start: 'busy', settle: 'idle' }
} satisfies TransitionTable
export const LOGIN = {
  callback: { validate: 'exchanging', fail: 'failed' },
  exchanging: { token: 'authenticated', fail: 'failed' }
} satisfies TransitionTable

export type ResourceState = 'loading' | 'failed' | 'ready' | 'unloaded'

export function resourceState({ pending = false, error = '', loaded = false } = {}): ResourceState {
  return pending ? 'loading' : error ? 'failed' : loaded ? 'ready' : 'unloaded'
}

export type RequestPhase = 'current' | 'stale'

export function requestPhase(current: boolean): RequestPhase { return current ? 'current' : 'stale' }

export interface Polling {
  readonly state: 'stopped' | 'running'
  start(): void
  stop(): void
}

export function createPolling(callback: () => unknown, interval: number): Polling {
  let timer: ReturnType<typeof setInterval> | null = null
  return {
    get state() { return timer === null ? 'stopped' : 'running' },
    start() { if (timer === null) timer = setInterval(callback, interval) },
    stop() { if (timer !== null) clearInterval(timer); timer = null }
  }
}
