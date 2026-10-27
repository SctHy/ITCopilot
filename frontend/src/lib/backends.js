import { pollEvaluation } from './evaluationJobs.js'
const DEFAULT_BACKENDS = {
  python: {
    id: 'python',
    label: 'Python',
    baseUrl: runtimeConfig().pythonApiUrl || import.meta.env?.VITE_PYTHON_API_URL || 'http://127.0.0.1:8001',
    port: '8001'
  },
  java: {
    id: 'java',
    label: 'Java',
    baseUrl: runtimeConfig().javaApiUrl || import.meta.env?.VITE_JAVA_API_URL || '/api/java',
    port: '8080'
  }
}

export function createInitialSettings() {
  const saved = readSettings()
  const savedPythonEndpoint = saved.endpoints?.python === '/api/python' ? 'http://127.0.0.1:8001' : saved.endpoints?.python
  return {
    pythonToken: '',
    backend: saved.backend || import.meta.env.VITE_DEFAULT_BACKEND || 'python',
    userId: saved.userId || 'u1001',
    conversationId: saved.conversationId || '',
    endpoints: {
      python: savedPythonEndpoint || DEFAULT_BACKENDS.python.baseUrl,
      java: saved.endpoints?.java || DEFAULT_BACKENDS.java.baseUrl
    }
  }
}

export function saveSettings(settings) {
  localStorage.setItem('it-copilot.frontend.settings', JSON.stringify({ ...settings, pythonToken: undefined }))
}

export function backendMeta(type, settings) {
  const meta = DEFAULT_BACKENDS[type] || DEFAULT_BACKENDS.java
  return {
    ...meta,
    baseUrl: normalizeBaseUrl(settings.endpoints[type] || meta.baseUrl)
  }
}

export async function requestHealth(type, settings) {
  return requestFor(type, settings, '/health')
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

export async function runEvaluation(type, settings, body = null, options = {}) {
  const taskKey = `it-copilot.eval:${type}:${backendMeta(type, settings).baseUrl}:${settings.userId}`
  const savedJob = type === 'python' ? sessionStorage.getItem(taskKey) : null
  const initial = savedJob ? {job_id: savedJob, status: 'queued', completed: 0, total: 0} : await requestFor(type, settings, '/eval/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined
  })
  if (!initial.job_id) return initial // Java 后端仍兼容同步报告。
  sessionStorage.setItem(taskKey, initial.job_id)
  try {
    const result = await pollEvaluation(initial, jobId => requestFor(type, settings,
      `/eval/jobs/${encodeURIComponent(jobId)}`), options)
    if (result) sessionStorage.removeItem(taskKey)
    return result
  } catch (error) {
    if (error.terminal || error.status === 404) sessionStorage.removeItem(taskKey)
    throw error
  }
}

export async function requestSearch(type, settings, query, topK = 5) {
  const params = new URLSearchParams({ query, top_k: String(topK) })
  return requestFor(type, settings, `/search?${params}`, { method: 'POST' })
}

export async function requestChat(type, settings, message) {
  const meta = backendMeta(type, settings)
  const payload = buildChatPayload(type, settings, message)
  const attemptKey = JSON.stringify([type, meta.baseUrl, settings.userId, settings.conversationId, message])
  if (type === 'python') {
    if (!chatAttempts.has(attemptKey)) {
      if (chatAttempts.size > 100) chatAttempts.clear()
      chatAttempts.set(attemptKey, crypto.randomUUID())
    }
    payload.client_request_id = chatAttempts.get(attemptKey)
  }
  const raw = await requestFor(type, settings, '/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  })
  chatAttempts.delete(attemptKey)
  return normalizeChatResponse(type, raw)
}

export async function requestChatStream(type, settings, message, onEvent = () => {}) {
  if (type !== 'python') return requestChat(type, settings, message)
  const meta = backendMeta(type, settings)
  const payload = buildChatPayload(type, settings, message)
  const attemptKey = JSON.stringify([type, meta.baseUrl, settings.userId, settings.conversationId, message])
  if (!chatAttempts.has(attemptKey)) chatAttempts.set(attemptKey, crypto.randomUUID())
  payload.client_request_id = chatAttempts.get(attemptKey)
  const headers = new Headers({ 'Content-Type': 'application/json', Accept: 'text/event-stream' })
  if (settings.pythonToken) headers.set('Authorization', `Bearer ${settings.pythonToken}`)
  const response = await fetch(`${normalizeBaseUrl(meta.baseUrl)}/chat/stream`, { method: 'POST', headers, body: JSON.stringify(payload), signal: AbortSignal.timeout(120000) })
  if (!response.ok || !response.body) { chatAttempts.delete(attemptKey); throw new Error(`${response.status} ${response.statusText}`) }
  const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = ''; let eventName = 'message'; let completed = null
  const consume = (chunk) => {
    buffer += decoder.decode(chunk, { stream: true }); const blocks = buffer.split('\n\n'); buffer = blocks.pop() || ''
    for (const block of blocks) {
      let data = ''
      for (const line of block.split('\n')) { if (line.startsWith('event:')) eventName = line.slice(6).trim(); if (line.startsWith('data:')) data += line.slice(5).trim() }
      if (!data) continue
      let parsed; try { parsed = JSON.parse(data) } catch { parsed = { raw: data } }
      onEvent(eventName, parsed); if (eventName === 'completed') completed = parsed; eventName = 'message'
    }
  }
  while (true) { const { value, done } = await reader.read(); if (done) break; consume(value) }
  chatAttempts.delete(attemptKey); if (!completed) throw new Error('流式响应未完成'); return normalizeChatResponse(type, completed)
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

export async function uploadKnowledge(type, settings, file) {
  const form = new FormData()
  form.append('file', file)
  return requestFor(type, settings, '/knowledge/upload', {
    method: 'POST',
    body: form
  })
}

function buildChatPayload(type, settings, message) {
  return {
    message,
    user_id: settings.userId || 'anonymous',
    conv_id: settings.conversationId || undefined
  }
}

function normalizeChatResponse(type, raw) {
  return {
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
  const headers = new Headers(options.headers || {})
  if (type === 'python' && settings.pythonToken) headers.set('Authorization', `Bearer ${settings.pythonToken}`)
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
