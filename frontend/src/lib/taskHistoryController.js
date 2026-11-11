import { reactive } from 'vue'

// Searches and continuation have separate state; identity/room remounts discard late results.
export function createTaskHistoryController(client, roomId = '', onOpen = () => {}) {
  const state = reactive({ query: '', items: [], links: [], busy: false, continuing: '', error: '' })
  let alive = true, revision = 0
  const keys = new Map()
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
      if (alive && at === revision) { state.error = error.message; state.items = []; state.links = [] }
    } finally { if (alive && at === revision) state.busy = false }
  }
  async function link(id, linked) {
    state.error = ''
    try { await client.capability(roomId, 'links', { target_id: id, linked }); if (alive) await search() }
    catch (error) { if (alive) state.error = error.message }
  }
  async function resume(item, message) {
    if (state.continuing) return
    const key = JSON.stringify([item.id, message])
    if (!keys.has(key)) keys.set(key, crypto.randomUUID())
    state.continuing = item.id; state.error = ''
    try {
      const result = await client.continue(item.id, { message, client_request_id: keys.get(key) })
      keys.delete(key)
      if (alive) onOpen(result.task_id)
    } catch (error) { if (alive) state.error = error.message }
    finally { if (alive) state.continuing = '' }
  }
  return { state, search, link, resume, dispose: () => { alive = false; revision++ } }
}
