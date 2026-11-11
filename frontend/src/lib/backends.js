import { storedToken } from './auth.js'
import { STREAM, machine } from './stateMachine.js'

const DEFAULT_BACKENDS = {
  python: {
    id: 'python',
    label: 'Python',
    baseUrl: runtimeConfig().pythonApiUrl || import.meta.env?.VITE_PYTHON_API_URL || 'http://127.0.0.1:8001',
    port: '8001'
  }
}

export function createInitialSettings() {
  const saved = readSettings()
  const savedPythonEndpoint = saved.endpoints?.python === '/api/python' ? 'http://127.0.0.1:8001' : saved.endpoints?.python
  return {
    // 身份由登录令牌决定（见 auth.js）；请求体不再携带 user_id / department。
    accessToken: typeof sessionStorage === 'undefined' ? '' : storedToken(),
    backend: 'python',
    conversationId: saved.conversationId || '',
    endpoints: {
      python: savedPythonEndpoint || DEFAULT_BACKENDS.python.baseUrl
    }
  }
}

export function saveSettings(settings) {
  localStorage.setItem('it-copilot.frontend.settings', JSON.stringify({ ...settings, accessToken: undefined }))
}

export function backendMeta(type, settings) {
  const meta = DEFAULT_BACKENDS[type] || DEFAULT_BACKENDS.python
  return {
    ...meta,
    baseUrl: normalizeBaseUrl(settings.endpoints[type] || meta.baseUrl)
  }
}

export async function requestHealth(type, settings) {
  return requestFor(type, settings, '/health')
}

export async function requestApproval(type, settings, id, decision, hash) {
  const path = `/approvals/${encodeURIComponent(id)}`
  return requestFor(type, settings, path, decision ? {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ decision, payload_hash: hash })
  } : {})
}

// 人工对账（IT 管理员）：结果未知或需复核的动作重新交给执行器，先回查目标系统再决定是否重做。
export async function reconcileApproval(type, settings, id) {
  return requestFor(type, settings, `/approvals/${encodeURIComponent(id)}/reconcile`, { method: 'POST' })
}

// 申请人撤回仍待审批的申请；已有结果的申请服务端返回 409。
export async function withdrawApproval(type, settings, id, reason = '') {
  return requestFor(type, settings, `/approvals/${encodeURIComponent(id)}/withdraw`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ reason })
  })
}

// 提前归还该申请开通的限时权限：到期回收立即执行，仍做漂移检查。
export async function releaseApproval(type, settings, id) {
  return requestFor(type, settings, `/approvals/${encodeURIComponent(id)}/release`, { method: 'POST' })
}

export async function listApprovals(type, settings, status = 'pending') {
  return requestFor(type, settings, `/approvals?status=${encodeURIComponent(status)}`)
}

export async function requestTaskInbox(type, settings, params = {}) {
  return requestFor(type, settings, '/task-inbox?' + new URLSearchParams(params))
}

export async function requestTaskHistory(type, settings, params = {}) {
  return requestFor(type, settings, '/task-history?' + new URLSearchParams(params))
}

export async function continueTask(type, settings, id, payload) {
  return requestFor(type, settings, `/task-history/${encodeURIComponent(id)}/continue`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
  })
}

// Shared rooms use server-side transcripts and durable request keys, not private chat memory.
export async function requestTasks(type, settings, taskId = '', suffix = '', payload = undefined) {
  const path = `/tasks${taskId ? `/${encodeURIComponent(taskId)}` : ''}${suffix}`
  return requestFor(type, settings, path, payload === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
  })
}

export async function requestAuthConfig(type, settings) {
  return requestFor(type, settings, '/auth/config')
}

export async function requestMe(type, settings) {
  return requestFor(type, settings, '/auth/me')
}

export async function requestDevToken(type, settings, username) {
  return requestFor(type, settings, '/auth/dev-token', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username })
  })
}

export async function requestMonitor(type, settings) {
  return requestFor(type, settings, '/monitor')
}

export async function requestSkills(type, settings) {
  return requestFor(type, settings, '/skills')
}

export async function reloadSkills(type, settings) {
  return requestFor(type, settings, '/skills/reload', { method: 'POST' })
}

export async function requestKnowledgeStats(type, settings) {
  return requestFor(type, settings, '/knowledge/stats')
}

// 离线评测报告（scripts/evaluate_all.py 生成）；只读，不在线触发 LLM 评测。
export async function requestEvaluationReports(type, settings) {
  return requestFor(type, settings, '/eval/reports')
}

export async function requestSearch(type, settings, query, topK = 5) {
  const params = new URLSearchParams({ query, top_k: String(topK) })
  const data = await requestFor(type, settings, `/search?${params}`, { method: 'POST' })
  if (data?.error) throw new Error(data.error)
  return { results: Array.isArray(data) ? data : (data?.results || data?.items || []) }
}

export async function requestChat(type, settings, message) {
  const meta = backendMeta(type, settings)
  const payload = buildChatPayload(type, settings, message)
  const attemptKey = JSON.stringify([type, meta.baseUrl, settings.accessToken, settings.conversationId, message])
  if (!chatAttempts.has(attemptKey)) {
    if (chatAttempts.size > 100) chatAttempts.clear()
    chatAttempts.set(attemptKey, crypto.randomUUID())
  }
  payload.client_request_id = chatAttempts.get(attemptKey)
  const raw = await requestFor(type, settings, '/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  })
  chatAttempts.delete(attemptKey)
  return normalizeChatResponse(type, raw)
}

// 实时事件流：每个 SSE 事件带 id；连接中断时用 Last-Event-ID 续读同一请求，服务端不会重复执行。
// 生命周期：连接 → 读事件 →（断线且已拿到 request_id：续读，最多 maxResumes 次）→ completed | error。
// 订阅超时不证明执行已停止；retry_same_request 时保留幂等键，重新发送续接原执行。
export async function requestChatStream(type, settings, message, onEvent = () => {}, { maxResumes = 3 } = {}) {
  const meta = backendMeta(type, settings)
  const payload = buildChatPayload(type, settings, message)
  const attemptKey = JSON.stringify([type, meta.baseUrl, settings.accessToken, settings.conversationId, message])
  if (!chatAttempts.has(attemptKey)) chatAttempts.set(attemptKey, crypto.randomUUID())
  payload.client_request_id = chatAttempts.get(attemptKey)
  const flow = machine(STREAM, 'opening')
  const state = { requestId: '', lastEventId: '', completed: null, failed: null }
  try {
    let response = await fetch(`${normalizeBaseUrl(meta.baseUrl)}/chat/stream`, {
      method: 'POST', headers: authHeaders(settings, { 'Content-Type': 'application/json', Accept: 'text/event-stream' }),
      body: JSON.stringify(payload), signal: AbortSignal.timeout(180000)
    })
    for (let resumes = 0; ; resumes++) {
      if (!response.ok) {
        // A server rejection is definitive; a broken transport is not.
        flow.send('fail')
        chatAttempts.delete(attemptKey)
        throw new Error(`${response.status} ${response.statusText}`)
      }
      if (!response.body) throw new Error('响应体缺失；再次发送将续接原请求')
      flow.send('connected')
      try {
        await readEventStream(response.body, (id, event, data) => {
          if (id) state.lastEventId = id
          if (event === 'status' && data.request_id) state.requestId = data.request_id
          if (event === 'completed') state.completed = data
          if (event === 'error') state.failed = data
          onEvent(event, data)
          return !state.completed && !state.failed
        })
      } catch (error) {
        if (!state.requestId || resumes >= maxResumes) throw error
      }
      if (state.completed) {
        flow.send('complete')
        chatAttempts.delete(attemptKey)
        return normalizeChatResponse(type, state.completed)
      }
      if (state.failed) {
        const interrupted = state.failed.retry_same_request === true
        flow.send(interrupted ? 'interrupt' : 'fail')
        if (!interrupted) chatAttempts.delete(attemptKey)
        if (state.failed.detail === 'stream_abandoned') {
          throw new Error(interrupted ? '等待响应超时，服务端结果尚未确认；再次发送将续接原请求' : '服务端处理中断，请重新发送')
        }
        throw new Error(`${state.failed.status_code || ''} ${state.failed.detail || '请求失败'}`.trim())
      }
      if (!state.requestId || resumes >= maxResumes) throw new Error('流式响应未完成；再次发送将续接原请求')
      flow.send('resume')
      onEvent('resumed', { request_id: state.requestId, last_event_id: state.lastEventId })
      response = await fetch(`${normalizeBaseUrl(meta.baseUrl)}/chat/stream/${encodeURIComponent(state.requestId)}`, {
        headers: authHeaders(settings, { Accept: 'text/event-stream', 'Last-Event-ID': state.lastEventId }), signal: AbortSignal.timeout(180000)
      })
    }
  } catch (error) {
    if (['opening', 'reading', 'resuming'].includes(flow.state)) flow.send('interrupt')
    // Keep the idempotency key for every ambiguous transport outcome.
    throw error
  }
}

export async function readEventStream(body, onEvent) {
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  const dispatch = (block) => {
    let id = ''; let event = 'message'; let data = ''
    for (const line of block.split('\n')) {
      if (line.startsWith('id:')) id = line.slice(3).trim()
      else if (line.startsWith('event:')) event = line.slice(6).trim()
      else if (line.startsWith('data:')) data += line.slice(5).trim()
    }
    if (!data) return  // 心跳注释行
    let parsed
    try { parsed = JSON.parse(data) } catch { parsed = { raw: data } }
    return onEvent(id, event, parsed)
  }
  try {
    while (true) {
      const { value, done } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const blocks = buffer.split('\n\n')
      buffer = blocks.pop() || ''
      for (const block of blocks) {
        if (dispatch(block) === false) { await reader.cancel(); return }
      }
    }
    if (buffer.trim()) dispatch(buffer)
  } finally {
    reader.releaseLock()
  }
}

export async function requestToolTrace(type, settings, requestId) {
  if (!requestId) return null
  const raw = await requestFor(type, settings, `/trace/tool/${encodeURIComponent(requestId)}`)
  return normalizeToolTraceResponse(raw)
}

export async function addKnowledge(type, settings, documents) {
  return requestFor(type, settings, '/knowledge/add', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ documents })
  })
}

export async function listKnowledgeDocuments(type, settings) {
  return requestFor(type, settings, '/knowledge/documents')
}

export async function deleteKnowledgeDocument(type, settings, documentId) {
  return requestFor(type, settings, `/knowledge/documents/${encodeURIComponent(documentId)}`, { method: 'DELETE' })
}

export async function uploadKnowledge(type, settings, file) {
  const form = new FormData()
  form.append('file', file)
  return requestFor(type, settings, '/knowledge/upload', {
    method: 'POST',
    body: form
  })
}

function buildChatPayload(type, settings, message) {
  return { message, conv_id: settings.conversationId || undefined }
}

function authHeaders(settings, extra = {}) {
  const headers = new Headers(extra)
  if (settings.accessToken) headers.set('Authorization', `Bearer ${settings.accessToken}`)
  return headers
}

function normalizeChatResponse(type, raw) {
  return {
    taskReferences: raw.task_references || [],
    relatedApprovals: raw.related_approvals || [],
    backend: type,
    conversationId: raw.conversation_id || raw.conversationId || raw.conv_id || '',
    requestId: raw.request_id || raw.requestId || '',
    response: raw.response || '',
    intent: raw.intent || 'other',
    intentGroup: raw.intent_group || raw.intentGroup || 'other',
    agentType: raw.agent_type || raw.agentType || '',
    agentTypes: raw.agent_types || raw.agentTypes || [],
    primaryAgent: raw.primary_agent || raw.primaryAgent || '',
    supportingAgents: raw.supporting_agents || raw.supportingAgents || [],
    routingReason: raw.routing_reason || raw.routingReason || '',
    routingConfidence: Number(raw.routing_confidence ?? raw.routingConfidence ?? raw.confidence ?? 0),
    entities: raw.entities || {},
    intentConfidence: Number(raw.intent_confidence ?? raw.intentConfidence ?? raw.confidence ?? 0),
    intentSourceScores: raw.intent_source_scores || raw.intentSourceScores || {},
    clarificationNeeded: Boolean(raw.clarification_needed ?? raw.clarificationNeeded),
    missingSlots: raw.missing_slots || raw.missingSlots || [],
    inheritedFromPrevious: Boolean(raw.inherited_from_previous ?? raw.inheritedFromPrevious),
    escalated: Boolean(raw.escalated),
    latencyMs: Number(raw.latency_ms ?? raw.latencyMs ?? 0),
    knowledgeUsed: Boolean(raw.knowledge_used ?? raw.knowledgeUsed),
    verified: raw.verified,
    grounded: raw.grounded,
    plan: raw.plan || null,
    findings: raw.findings || [],
    approvalRequired: Boolean(raw.approval_required ?? raw.approvalRequired),
    approvalId: raw.approval_id || raw.approvalId || null,
    tokenUsage: raw.token_usage || raw.tokenUsage || {},
    raw
  }
}

export function normalizeToolTraceResponse(raw) {
  const trace = raw?.trace || {}
  return {
    requestId: raw?.request_id || raw?.requestId || '',
    found: Boolean(raw?.found),
    trace: {
      ...trace,
      toolsUsed: trace.tools_used || trace.toolsUsed || [],
      toolCalls: trace.tool_traces || trace.tool_calls || trace.toolCalls || []
    },
    raw
  }
}

const chatAttempts = new Map()
async function requestFor(type, settings, path, options = {}) {
  const headers = authHeaders(settings, options.headers || {})
  return requestJson(backendMeta(type, settings).baseUrl, path, { ...options, headers,
    signal: options.signal || AbortSignal.timeout(path === '/chat' || path === '/chat/stream' ? 120000 : 20000) })
}

async function requestJson(baseUrl, path, options = {}) {
  const url = `${normalizeBaseUrl(baseUrl)}${path}`
  const response = await fetch(url, options)
  const text = await response.text()
  let data = null
  try {
    data = text ? JSON.parse(text) : null
  } catch {
    data = text
  }
  if (!response.ok) {
    const detail = typeof data === 'string' ? data : JSON.stringify(data)
    const error = new Error(`${response.status} ${response.statusText}: ${detail}`)
    error.status = response.status
    throw error
  }
  return data
}

function normalizeBaseUrl(value) {
  return String(value || '').replace(/\/+$/, '')
}

function readSettings() {
  try {
    return JSON.parse(localStorage.getItem('it-copilot.frontend.settings') || '{}')
  } catch {
    return {}
  }
}

function runtimeConfig() {
  if (typeof window === 'undefined') return {}
  return window.__IT_COPILOT_CONFIG__ || {}
}

export async function uploadTaskFile(type, settings, id, file, requestId) {
  const path = `/tasks/${encodeURIComponent(id)}/files?` + new URLSearchParams({ name: file.name, client_request_id: requestId })
  return requestFor(type, settings, path, { method: 'POST', body: file, headers: { 'Content-Type': 'application/octet-stream' } })
}
export async function downloadTaskFile(type, settings, id, fileId) {
  const response = await fetch(backendMeta(type, settings).baseUrl + `/tasks/${encodeURIComponent(id)}/files/${encodeURIComponent(fileId)}`, {
    headers: authHeaders(settings), signal: AbortSignal.timeout(30000)
  })
  if (!response.ok) throw new Error('下载失败，请检查事项访问权限')
  return response.blob()
}
