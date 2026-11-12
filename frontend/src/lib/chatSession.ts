// Private chat session: message list, streaming status and the last request's details.
// Transport is injected (no fetch here) and DOM concerns such as scrolling stay in the view.
import { reactive } from 'vue'
import { createAnswerDraft } from './answerDraft.ts'
import type { RequestState, TrackedRequest } from './requestState.ts'
import type { ChatResponse, StreamEventHandler } from '../api/chat.ts'
import type { ApprovalItem, ChatMessage, RelatedApproval, Settings } from './types.ts'

export interface ChatTransport {
  stream(type: string, settings: Settings, message: string, onEvent: StreamEventHandler): Promise<ChatResponse>
  trace(type: string, settings: Settings, requestId: string): Promise<Record<string, any> | null>
  /** Fill in an approval card attached to a reply (mutates the card's approval/busy/error fields). */
  loadApproval(card: ApprovalItem): Promise<void>
}

export interface ChatSessionState {
  messages: ChatMessage[]
  draft: string
  busy: boolean
  streamStatus: string
  lastResponse: ChatResponse | null
  lastTrace: Record<string, any> | null
}

export interface ChatSession {
  state: ChatSessionState
  send(): Promise<void>
  /** Start a new conversation: drops messages and the conversation id. */
  clear(): void
  /** Drop what is on screen but keep the conversation id (used when the connection changes). */
  reset(): void
}

const streamLabels: Record<string, (data: Record<string, any>) => string> = {
  status: () => '已受理，正在识别问题',
  intent: data => `已识别：${data.intent}`,
  tool_started: data => `正在调用工具 ${data.tool}`,
  tool_finished: data => `工具 ${data.tool} ${data.success ? '完成' : '失败'}（${data.latency_ms}ms）`,
  approval_required: data => `已提交审批，需 ${data.approvals_required} 人批准`,
  content_delta: () => '正在输出回复',
  resumed: () => '连接中断，正在续传',
  answer_reset: () => '正在调用工具'
}
const intentLabels: Record<string, string> = { greeting: '打招呼', chitchat: '闲聊', out_of_scope: '超出范围', vpn_issue: 'VPN 故障', password_reset: '密码重置', permission_request: '权限申请', other: '其他' }

/** The one-line summary shown above an assistant reply. */
export function replyMeta(response: ChatResponse): string {
  const intentLabel = intentLabels[response.intent] || response.intent || '其他'
  const confidence = Math.round(Math.max(0, Math.min(1, Number(response.intentConfidence ?? 0))) * 100)
  return [
    `${intentLabel} · 置信度 ${confidence}%`,
    response.clarificationNeeded ? `待补充：${(response.missingSlots || []).join('、')}` : '',
    response.approvalRequired ? `待审批 · ${response.approvalId || ''}` : '',
    response.knowledgeUsed ? 'RAG' : '',
    response.escalated ? '转人工' : ''
  ].filter(Boolean).join(' · ')
}

export function createChatSession(settings: Settings, requestState: RequestState, transport: ChatTransport,
  hooks: { persist(): void; onReply?(): unknown } ): ChatSession {
  const state = reactive<ChatSessionState>({ messages: [], draft: '', busy: false, streamStatus: '', lastResponse: null, lastTrace: null })
  let sequence = 0
  const createId = () => `message-${Date.now()}-${++sequence}`
  const errorMessage = (error: unknown) => error instanceof Error ? error.message : String(error)

  async function loadTrace(requestId: string, request: TrackedRequest) {
    try {
      return await transport.trace(request.backend, request.settings, requestId)
    } catch {
      return null
    }
  }

  async function send() {
    const content = state.draft.trim()
    if (!content || state.busy) return
    state.messages.push({ id: createId(), role: 'user', content })
    state.draft = ''
    state.lastResponse = null
    state.lastTrace = null
    const request = requestState.begin('chat')
    state.busy = true
    state.streamStatus = ''
    const answerDraft = createAnswerDraft(state.messages, createId)
    try {
      if (!settings.conversationId) {
        settings.conversationId = crypto.randomUUID()
        hooks.persist()
      }
      const response = await transport.stream(request.backend, { ...request.settings, conversationId: settings.conversationId }, content, (event, data) => {
        // 执行过程中实时到达的事件（不是完成后回放）。
        if (!request.isCurrent()) return
        if (answerDraft.onEvent(event, data)) return
        state.streamStatus = streamLabels[event]?.(data) || state.streamStatus
      })
      if (!request.isCurrent()) return
      if (response.conversationId && !settings.conversationId) {
        settings.conversationId = response.conversationId
        hooks.persist()
      }
      const trace = await loadTrace(response.requestId, request)
      if (!request.isCurrent()) return
      answerDraft.remove()
      state.lastResponse = response
      state.lastTrace = trace
      // The caller's own withdrawable/releasable items: shown as cards so taking something back is always a click.
      const related: RelatedApproval[] = response.relatedApprovals.map(entry => ({
        approvalId: entry.approval_id, kind: entry.kind, summary: entry.summary,
        approval: null, approvalSettings: request.settings, approvalBackend: request.backend }))
      state.messages.push({ id: createId(), role: 'assistant', content: response.response, meta: replyMeta(response),
        trace: trace?.trace || null, taskReferences: response.taskReferences,
        approvalId: response.approvalId ?? undefined, approvalSettings: request.settings, approvalBackend: request.backend,
        relatedApprovals: related })
      // Read back the reactive proxy so the loads below update what is rendered.
      const pushed = state.messages[state.messages.length - 1]!
      for (const entry of pushed.relatedApprovals || []) transport.loadApproval(entry)
      await hooks.onReply?.()
    } catch (error) {
      if (!request.isCurrent()) return
      answerDraft.remove()
      state.messages.push({ id: createId(), role: 'assistant', content: errorMessage(error), meta: '请求失败', error: true })
    } finally {
      if (request.isCurrent()) {
        state.busy = false
        state.streamStatus = ''
      }
    }
  }

  function reset() {
    requestState.invalidate()
    state.busy = false
    state.streamStatus = ''
    state.messages = []
    state.lastResponse = null
    state.lastTrace = null
  }

  function clear() {
    reset()
    settings.conversationId = ''
    hooks.persist()
  }

  return { state, send, clear, reset }
}
