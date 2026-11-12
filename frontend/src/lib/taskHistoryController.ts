import { reactive } from 'vue'
import type { TaskClient } from './taskClient.ts'

export type TaskHistoryTransport = Pick<TaskClient, 'history' | 'linked' | 'capability' | 'continue'>

export interface TaskHistoryItem {
  id: string
  title: string
  status: string
  steps_done: number
  steps_total: number
  pending_todos: number
  active_runs: number
  goal?: string
  blocker?: string
  next_action?: string
  resolution?: string
  can_continue?: boolean
  approvals?: { approval_id: string; status: string; execution: string }[]
}

const message = (error: unknown) => error instanceof Error ? error.message : String(error)

// Searches and continuation have separate state; identity/room remounts discard late results.
export function createTaskHistoryController(client: TaskHistoryTransport, roomId = '', onOpen: (id: string) => void = () => {}) {
  const state = reactive({ query: '', items: [] as TaskHistoryItem[], links: [] as TaskHistoryItem[], busy: false, continuing: '', error: '' })
  let alive = true, revision = 0
  const keys = new Map<string, string>()
  async function search() {
    const at = ++revision
    state.busy = true; state.error = ''
    try {
      const [found, linked] = await Promise.all([
        client.history({ q: state.query.trim(), limit: 8, ...(roomId ? { exclude_id: roomId } : {}) }),
        roomId ? client.linked(roomId) : Promise.resolve({ items: [] })
      ])
      if (alive && at === revision) { state.items = found.items; state.links = linked.items }
    } catch (error) {
      if (alive && at === revision) { state.error = message(error); state.items = []; state.links = [] }
    } finally { if (alive && at === revision) state.busy = false }
  }
  async function link(id: string, linked: boolean) {
    state.error = ''
    try { await client.capability(roomId, 'links', { target_id: id, linked }); if (alive) await search() }
    catch (error) { if (alive) state.error = message(error) }
  }
  async function resume(item: TaskHistoryItem, text: string) {
    if (state.continuing) return
    const key = JSON.stringify([item.id, text])
    if (!keys.has(key)) keys.set(key, crypto.randomUUID())
    state.continuing = item.id; state.error = ''
    try {
      const result = await client.continue(item.id, { message: text, client_request_id: keys.get(key) })
      keys.delete(key)
      if (alive) onOpen(result.task_id)
    } catch (error) { if (alive) state.error = message(error) }
    finally { if (alive) state.continuing = '' }
  }
  return { state, search, link, resume, dispose: () => { alive = false; revision++ } }
}
