// Session state with an injected transport; no fetch, approval rules or DOM lifecycle.
import { reactive } from 'vue'

export function createTaskController(client) {
  const state = reactive({ items: [], room: null, selected: '', creating: false, title: '', initialMessage: '',
    message: '', askAgent: true, busy: false, error: '', notice: '', approvalBusy: {}, approvalErrors: {}, query: { q: '', status: '', archived: false, category: 'all', offset: 0, limit: 30 }, total: 0, hasMore: false })
  let alive = true, generation = 0, refreshing = false, refreshAgain = false
  const attempts = new Map()

  async function refresh() {
    if (!alive) return
    if (refreshing) { refreshAgain = true; return }
    refreshing = true
    const at = generation, id = state.selected
    try {
      const [list, current] = await Promise.all([client.list({ ...state.query }), id ? client.get(id) : Promise.resolve(null)])
      if (alive && at === generation) {
        state.items = list.items || []; state.total = list.total || 0; state.hasMore = !!list.has_more
        // A long absence can leave a gap beyond the latest page. Start from that
        // page again so "older" can reach the missing interval in sequence.
        const overlap = current?.events.some(e => state.room?.events.some(old => old.id === e.id))
        if (current && state.room?.id === current.id && overlap) {
          const events = new Map([...state.room.events, ...current.events].map(e => [e.id, e]))
          current.events = [...events.values()].sort((a, b) => a.seq - b.seq)
          if (state.room.has_more_events === false) current.has_more_events = false
        }
        state.room = current
      }
    } catch (error) {
      if (alive && at === generation) { state.error = error.message; state.room = null }
    } finally {
      refreshing = false
      if (refreshAgain && alive) { refreshAgain = false; await refresh() }
    }
  }

  function select(id) {
    if (state.busy) return
    generation++; state.selected = id; state.room = null; state.creating = false; state.message = ''; state.error = ''
    state.approvalBusy = {}; state.approvalErrors = {}
    return refresh()
  }
  function beginNew() {
    if (state.busy) return
    generation++; state.selected = ''; state.room = null; state.creating = true; state.error = ''
  }
  async function mutate(action) {
    if (state.busy) return
    state.busy = true; state.error = ''; state.notice = ''
    try { await action() } catch (error) { if (alive) state.error = error.message }
    finally { if (alive) { state.busy = false; await refresh() } }
  }
  async function submitOnce(kind, payload, action) {
    const key = JSON.stringify([kind, state.selected, payload])
    if (!attempts.has(key)) attempts.set(key, crypto.randomUUID())
    const result = await action({ ...payload, client_request_id: attempts.get(key) })
    attempts.delete(key)
    return result
  }
  function create() {
    return mutate(async () => {
      const created = await submitOnce('create', { title: state.title.trim(), message: state.initialMessage.trim() }, client.create)
      if (!alive) return
      generation++; state.selected = created.id; state.room = created; state.creating = false; state.title = ''; state.initialMessage = ''
    })
  }
  function send() {
    const id = state.selected, text = state.message.trim()
    return mutate(async () => {
      await submitOnce('message', { message: text, ask_agent: state.askAgent }, payload => client.post(id, payload))
      if (alive && state.selected === id && state.message.trim() === text) state.message = ''
    })
  }
  async function decide(approval, decision) {
    const id = state.selected, at = generation, approvalId = approval.approval_id
    if (![undefined, 'approved', 'rejected', 'reconcile', 'withdraw', 'release'].includes(decision)) return
    if (state.approvalBusy[approvalId]) return
    state.approvalBusy[approvalId] = true; state.approvalErrors[approvalId] = ''
    try {
      if (decision === 'reconcile') await client.reconcile(approval)
      else if (decision === 'withdraw') await client.withdraw(approval)
      else if (decision === 'release') await client.release(approval)
      else if (decision) await client.decide(id, approval, decision)
    } catch (error) {
      if (alive && at === generation) state.approvalErrors[approvalId] = error.message
    } finally {
      if (alive && at === generation) { state.approvalBusy[approvalId] = false; await refresh() }
    }
  }
  function search(patch = {}) { generation++; Object.assign(state.query, patch); return refresh() }
  async function older() {
    if (!state.room?.events.length) return
    const id = state.selected, at = generation
    try {
      const result = await client.events(id, state.room.events[0].seq)
      if (alive && at === generation && state.room?.id === id) {
        const events = new Map([...result.items, ...state.room.events].map(e => [e.id, e]))
        state.room.events = [...events.values()].sort((a, b) => a.seq - b.seq)
        state.room.has_more_events = result.has_more
      }
    } catch (e) { if (alive && at === generation) state.error = e.message }
  }
  return { state, refresh, search, older, select, beginNew, create, send, decide,
    retry: run => mutate(() => client.retry(state.selected, run)),
    stop: () => mutate(() => client.stop(state.selected)),
    dispose: () => { alive = false; generation++ } }
}
