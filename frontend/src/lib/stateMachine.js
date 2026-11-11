// Event tables for transient UI protocols. Durable data stays in its original store.
export function transition(table, state, event) {
  const next = table[state]?.[event]
  if (!next) throw new Error(`Illegal transition: ${state} + ${event}`)
  return next
}

export function machine(table, initial) {
  let state = initial
  return { get state() { return state }, send(event) { state = transition(table, state, event); return state } }
}

export const STREAM = {
  opening: { connected: 'reading', interrupt: 'interrupted', fail: 'failed' },
  reading: { resume: 'resuming', complete: 'completed', fail: 'failed', interrupt: 'interrupted' },
  resuming: { connected: 'reading', interrupt: 'interrupted', fail: 'failed' }
}
export const DRAFT = {
  absent: { delta: 'writing', reset: 'absent', remove: 'removed' },
  writing: { delta: 'writing', reset: 'empty', remove: 'removed' },
  empty: { delta: 'writing', reset: 'empty', remove: 'removed' },
  removed: { remove: 'removed' }
}
export const ACTIVITY = {
  idle: { start: 'busy', settle: 'idle' },
  busy: { start: 'busy', settle: 'idle' }
}
export const LOGIN = {
  callback: { validate: 'exchanging', fail: 'failed' },
  exchanging: { token: 'authenticated', fail: 'failed' }
}

export function resourceState({ pending = false, error = '', loaded = false } = {}) {
  return pending ? 'loading' : error ? 'failed' : loaded ? 'ready' : 'unloaded'
}

export function requestPhase(current) { return current ? 'current' : 'stale' }

export function createPolling(callback, interval) {
  let timer = null
  return {
    get state() { return timer === null ? 'stopped' : 'running' },
    start() { if (timer === null) timer = setInterval(callback, interval) },
    stop() { if (timer !== null) clearInterval(timer); timer = null }
  }
}
