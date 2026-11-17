import assert from 'node:assert/strict'
import type { TaskClient } from '../src/lib/taskClient.ts'
import type { TaskEvent, TaskListItem, TaskRoom, TaskWorkspaceData } from '../src/lib/types.ts'

export function deferred<T = void>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

export function taskEvent(id: string, seq: number, kind = 'message'): TaskEvent {
  return { id, seq, kind, actor: 'alice', actor_name: 'Alice', text: '', created_at: seq }
}

export function taskRoom(id: string): TaskRoom {
  return { id, title: id, requester: 'alice', requester_name: 'Alice', status: 'open',
    phase: 'waiting', can_write: true, events: [], has_more_events: false }
}

export function taskItem(id: string): TaskListItem {
  return { id, title: id, requester_name: 'Alice', status: 'open' }
}

export function workspace(goal: string): TaskWorkspaceData {
  return { progress: { version: 1, goal, steps: [] }, todos: [], reminders: [], attempts: [],
    budget: { used_runs: 0, run_limit: 5, used_tokens: 0, token_limit: 100 } }
}

function unexpected(): never { throw new Error('Unexpected task client call') }

export function taskClient(overrides: Partial<TaskClient>): TaskClient {
  return {
    history: unexpected, linked: unexpected, continue: unexpected, list: unexpected,
    get: unexpected, create: unexpected, directory: unexpected, events: unexpected,
    upload: unexpected, download: unexpected, workspace: unexpected, capability: unexpected,
    post: unexpected, decide: unexpected, reconcile: unexpected, confirm: unexpected,
    withdraw: unexpected, release: unexpected, resolve: unexpected, retry: unexpected, stop: unexpected,
    ...overrides,
  }
}

export function requestKey(payload: unknown): string {
  assert.ok(payload && typeof payload === 'object' && 'client_request_id' in payload)
  assert.ok(typeof payload.client_request_id === 'string' && payload.client_request_id.length > 0)
  return payload.client_request_id
}
