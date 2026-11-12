// Chat transport: one-shot /chat, the resumable SSE stream, and normalisation of both into ChatResponse.
import { STREAM, machine } from '../lib/stateMachine.ts'
import { authHeaders, backendMeta, jsonBody, requestFor } from './http.ts'
import type { Settings } from '../lib/types.ts'

export type StreamEventHandler = (event: string, data: any) => void

export interface ChatResponse {
  taskReferences: { id: string; title: string }[]
  relatedApprovals: { approval_id: string; kind: string; summary: string }[]
  backend: string
  conversationId: string
  requestId: string
  response: string
  intent: string
  intentGroup: string
  toolsUsed: string[]
  entities: Record<string, unknown>
  intentConfidence: number
  intentSourceScores: Record<string, number>
  clarificationNeeded: boolean
  missingSlots: string[]
  inheritedFromPrevious: boolean
  escalated: boolean
  latencyMs: number
  knowledgeUsed: boolean
  verified: unknown
  grounded: unknown
  approvalRequired: boolean
  approvalId: string | null
  tokenUsage: Record<string, number>
  raw: any
}

// Idempotency keys per (connection, identity, conversation, message): an ambiguous failure keeps the key,
// so resending the same message continues the original execution instead of starting a second one.
const chatAttempts = new Map<string, string>()

function attemptKeyFor(type: string, settings: Settings, message: string) {
  const key = JSON.stringify([type, backendMeta(type, settings).baseUrl, settings.accessToken, settings.conversationId, message])
  if (!chatAttempts.has(key)) {
    if (chatAttempts.size > 100) chatAttempts.clear()
    chatAttempts.set(key, crypto.randomUUID())
  }
  return key
}

function buildChatPayload(settings: Settings, message: string, attemptKey: string) {
  return { message, conv_id: settings.conversationId || undefined, client_request_id: chatAttempts.get(attemptKey) }
}

export async function requestChat(type: string, settings: Settings, message: string): Promise<ChatResponse> {
  const attemptKey = attemptKeyFor(type, settings, message)
  const raw = await requestFor(type, settings, '/chat', jsonBody(buildChatPayload(settings, message, attemptKey)))
  chatAttempts.delete(attemptKey)
  return normalizeChatResponse(type, raw)
}

// 实时事件流：每个 SSE 事件带 id；连接中断时用 Last-Event-ID 续读同一请求，服务端不会重复执行。
// 生命周期：连接 → 读事件 →（断线且已拿到 request_id：续读，最多 maxResumes 次）→ completed | error。
// 订阅超时不证明执行已停止；retry_same_request 时保留幂等键，重新发送续接原执行。
export async function requestChatStream(type: string, settings: Settings, message: string,
  onEvent: StreamEventHandler = () => {}, { maxResumes = 3 } = {}): Promise<ChatResponse> {
  const baseUrl = backendMeta(type, settings).baseUrl
  const attemptKey = attemptKeyFor(type, settings, message)
  const payload = buildChatPayload(settings, message, attemptKey)
  const flow = machine(STREAM, 'opening')
  const state: { requestId: string; lastEventId: string; completed: any; failed: any } = { requestId: '', lastEventId: '', completed: null, failed: null }
  try {
    let response = await fetch(`${baseUrl}/chat/stream`, {
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
      response = await fetch(`${baseUrl}/chat/stream/${encodeURIComponent(state.requestId)}`, {
        headers: authHeaders(settings, { Accept: 'text/event-stream', 'Last-Event-ID': state.lastEventId }), signal: AbortSignal.timeout(180000)
      })
    }
  } catch (error) {
    if (['opening', 'reading', 'resuming'].includes(flow.state)) flow.send('interrupt')
    // Keep the idempotency key for every ambiguous transport outcome.
    throw error
  }
}

/** Parse an SSE body. Returning `false` from `onEvent` stops reading and cancels the stream. */
export async function readEventStream(body: ReadableStream<Uint8Array>, onEvent: (id: string, event: string, data: any) => boolean | void) {
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  const dispatch = (block: string) => {
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

export function normalizeChatResponse(type: string, raw: any): ChatResponse {
  return {
    taskReferences: raw.task_references || [],
    relatedApprovals: raw.related_approvals || [],
    backend: type,
    conversationId: raw.conversation_id || raw.conversationId || raw.conv_id || '',
    requestId: raw.request_id || raw.requestId || '',
    response: raw.response || '',
    intent: raw.intent || 'other',
    intentGroup: raw.intent_group || raw.intentGroup || 'other',
    toolsUsed: raw.tools_used || raw.toolsUsed || [],
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
    approvalRequired: Boolean(raw.approval_required ?? raw.approvalRequired),
    approvalId: raw.approval_id || raw.approvalId || null,
    tokenUsage: raw.token_usage || raw.tokenUsage || {},
    raw
  }
}

export async function requestToolTrace(type: string, settings: Settings, requestId: string) {
  if (!requestId) return null
  const raw = await requestFor(type, settings, `/trace/tool/${encodeURIComponent(requestId)}`)
  return normalizeToolTraceResponse(raw)
}

export function normalizeToolTraceResponse(raw: any) {
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
