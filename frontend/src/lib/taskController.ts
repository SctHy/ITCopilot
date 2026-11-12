// Session state with an injected transport; no fetch, approval rules or DOM lifecycle.
import { reactive } from 'vue'
import type { TaskClient } from './taskClient.ts'
import type { Approval, ApprovalDecision, TaskListItem, TaskQuery, TaskRoom } from './types.ts'

export type TaskTransport = Pick<TaskClient, 'list' | 'get' | 'create' | 'post' | 'events' | 'decide' | 'reconcile' | 'confirm' | 'withdraw' | 'release' | 'retry' | 'stop'>

export interface TaskControllerState {
  items: TaskListItem[]
  room: TaskRoom | null
  selected: string
  creating: boolean
  title: string
  initialMessage: string
  message: string
  askAgent: boolean
  busy: boolean
  error: string
  notice: string
  approvalBusy: Record<string, boolean>
  approvalErrors: Record<string, string>
  query: TaskQuery
  total: number
  hasMore: boolean
}

const message = (error: unknown) => error instanceof Error ? error.message : String(error)

export function createTaskController(client: TaskTransport) {
  const state = reactive<TaskControllerState>({ items: [], room: null, selected: '', creating: false, title: '', initialMessage: '',
    message: '', askAgent: true, busy: false, error: '', notice: '', approvalBusy: {}, approvalErrors: {},
    query: { q: '', status: '', archived: false, category: 'all', offset: 0, limit: 30 }, total: 0, hasMore: false })
  let alive = true, generation = 0, refreshing = false, refreshAgain = false, refreshFailed = false
  const attempts = new Map<string, string>()

  async function refresh(): Promise<void> {
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
        const loaded = state.room
        const overlap = current?.events.some(e => loaded?.events.some(old => old.id === e.id))
        if (current && loaded?.id === current.id && overlap) {
          const events = new Map([...loaded.events, ...current.events].map(e => [e.id, e]))
          current.events = [...events.values()].sort((a, b) => a.seq - b.seq)
          if (loaded.has_more_events === false) current.has_more_events = false
        }
        state.room = current
        if (refreshFailed) state.error = ''
        refreshFailed = false
      }
    } catch (error) {
      if (alive && at === generation) {
        state.error = message(error); refreshFailed = true
        // Only a lost room (no access / deleted) closes it. A transient poll failure keeps the room
        // mounted, so drafts in its panels and loaded older pages survive the next successful read.
        const status = (error as { status?: number }).status
        if (status === 403 || status === 404) state.room = null
      }
    } finally {
      refreshing = false
      if (refreshAgain && alive) { refreshAgain = false; await refresh() }
    }
  }

  function select(id: string) {
    if (state.busy) return
    generation++; state.selected = id; state.room = null; state.creating = false; state.message = ''; state.error = ''
    state.approvalBusy = {}; state.approvalErrors = {}
    return refresh()
  }
  function beginNew() {
    if (state.busy) return
    generation++; state.selected = ''; state.room = null; state.creating = true; state.error = ''
  }
  async function mutate(action: () => Promise<unknown>) {
    if (state.busy) return
    state.busy = true; state.error = ''; state.notice = ''; refreshFailed = false
    try { await action() } catch (error) { if (alive) state.error = message(error) }
    finally { if (alive) { state.busy = false; await refresh() } }
  }
  // A lost response keeps its request key, so the retry is recognised by the server as the same write.
  async function submitOnce<T>(kind: string, payload: Record<string, unknown>, action: (payload: Record<string, unknown>) => Promise<T>) {
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
  async function decide(approval: Approval, decision?: ApprovalDecision) {
    const id = state.selected, at = generation, approvalId = approval.approval_id
    if (![undefined, 'approved', 'rejected', 'reconcile', 'confirm', 'withdraw', 'release'].includes(decision)) return
    if (state.approvalBusy[approvalId]) return
    state.approvalBusy[approvalId] = true; state.approvalErrors[approvalId] = ''
    try {
      if (decision === 'reconcile') await client.reconcile(approval)
      else if (decision === 'confirm') await client.confirm(approval)
      else if (decision === 'withdraw') await client.withdraw(approval)
      else if (decision === 'release') await client.release(approval)
      else if (decision) await client.decide(id, approval, decision)
    } catch (error) {
      if (alive && at === generation) state.approvalErrors[approvalId] = message(error)
    } finally {
      if (alive && at === generation) { state.approvalBusy[approvalId] = false; await refresh() }
    }
  }
  function search(patch: Partial<TaskQuery> = {}) { generation++; Object.assign(state.query, patch); return refresh() }
  async function older() {
    const first = state.room?.events[0]
    if (!first) return
    const id = state.selected, at = generation
    try {
      const result = await client.events(id, first.seq)
      const room = state.room
      if (alive && at === generation && room?.id === id) {
        const events = new Map([...result.items, ...room.events].map(e => [e.id, e]))
        room.events = [...events.values()].sort((a, b) => a.seq - b.seq)
        room.has_more_events = !!result.has_more
      }
    } catch (e) { if (alive && at === generation) state.error = message(e) }
  }
  return { state, refresh, search, older, select, beginNew, create, send, decide,
    retry: (run: string) => mutate(() => client.retry(state.selected, run)),
    stop: () => mutate(() => client.stop(state.selected)),
    dispose: () => { alive = false; generation++ } }
}

export type TaskController = ReturnType<typeof createTaskController>
