import { reactive } from 'vue'
import type { TaskClient } from './taskClient.ts'
import type { TaskWorkspaceData } from './types.ts'

export type TaskDetailsTransport = Pick<TaskClient, 'workspace' | 'capability'>

const message = (error: unknown) => error instanceof Error ? error.message : String(error)

// Each workspace instance owns its transport and id; disposing fences late results.
export function createTaskDetailsController(client: TaskDetailsTransport, id: string) {
  const state = reactive<{ data: TaskWorkspaceData | null; error: string; busy: Record<string, boolean> }>({ data: null, error: '', busy: {} })
  let alive = true, refreshing = false, refreshAgain = false, refreshFailed = false
  const keys = new Map<string, string>()
  // One read at a time: with a 2s poll, superseding in-flight reads would starve a backend slower than
  // the poll interval. A call made during a read schedules one more read after it.
  async function refresh(): Promise<void> {
    if (!alive) return
    if (refreshing) { refreshAgain = true; return }
    refreshing = true
    try {
      const data = await client.workspace(id) as TaskWorkspaceData
      if (alive) { state.data = data; if (refreshFailed) state.error = '' }
      refreshFailed = false
    } catch (e) {
      if (alive) { state.error = message(e); refreshFailed = true }
    } finally {
      refreshing = false
      if (refreshAgain && alive) { refreshAgain = false; await refresh() }
    }
  }
  /** Runs a workspace capability once per (action, payload); resolves whether the view is still alive and it succeeded. */
  async function perform(action: string, payload: Record<string, unknown> = {}): Promise<boolean> {
    if (state.busy[action]) return false
    state.busy[action] = true; state.error = ''; refreshFailed = false
    const key = JSON.stringify([action, payload])
    if (!keys.has(key)) keys.set(key, crypto.randomUUID())
    try {
      await client.capability(id, action, { ...payload, client_request_id: keys.get(key) })
      keys.delete(key)
      if (alive) await refresh()
      return alive
    } catch (e) { if (alive) state.error = message(e); return false }
    finally { if (alive) state.busy[action] = false }
  }
  return { state, refresh, perform, dispose: () => { alive = false } }
}

export type TaskAct = (action: string, payload?: Record<string, unknown>) => Promise<boolean>
