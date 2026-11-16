import { useCallback, useEffect, useRef, useSyncExternalStore } from 'react'
import { subscribe, version } from '../lib/store.ts'
import { createPolling } from '../lib/stateMachine.ts'

/** Re-render whenever anything inside a `reactive()` object changes; returns the same object. */
export function useStore<T extends object>(state: T): T {
  const listen = useCallback((onChange: () => void) => subscribe(state, onChange), [state])
  useSyncExternalStore(listen, () => version(state))
  return state
}

/** Run `callback` now and then every `interval` ms while mounted. */
export function usePolling(callback: () => unknown, interval: number, immediate = true) {
  const latest = useRef(callback)
  latest.current = callback
  useEffect(() => {
    if (immediate) latest.current()
    const polling = createPolling(() => latest.current(), interval)
    polling.start()
    return () => polling.stop()
  }, [interval, immediate])
}
