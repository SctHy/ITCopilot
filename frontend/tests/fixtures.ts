import assert from 'node:assert/strict'

export function deferred<T = void>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}


export function requestKey(payload: unknown): string {
  assert.ok(payload && typeof payload === 'object' && 'client_request_id' in payload)
  assert.ok(typeof payload.client_request_id === 'string' && payload.client_request_id.length > 0)
  return payload.client_request_id
}
