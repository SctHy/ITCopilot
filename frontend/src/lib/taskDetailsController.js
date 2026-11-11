import { reactive } from 'vue'

// Each workspace instance owns its transport and id; disposing fences late results.
export function createTaskDetailsController(client, id) {
  const state = reactive({ data: null, error: '', busy: {} })
  let alive = true, revision = 0
  const keys = new Map()
  async function refresh() {
    const at = ++revision
    try { const data = await client.workspace(id); if (alive && at === revision) state.data = data }
    catch (e) { if (alive && at === revision) state.error = e.message }
  }
  async function perform(action, payload = {}) {
    if (state.busy[action]) return false
    state.busy[action] = true; state.error = ''
    const key = JSON.stringify([action, payload])
    if (!keys.has(key)) keys.set(key, crypto.randomUUID())
    try {
      await client.capability(id, action, { ...payload, client_request_id: keys.get(key) })
      keys.delete(key)
      if (alive) await refresh()
      return alive
    } catch (e) { if (alive) state.error = e.message; return false }
    finally { if (alive) state.busy[action] = false }
  }
  return { state, refresh, perform, dispose: () => { alive = false; revision++ } }
}
