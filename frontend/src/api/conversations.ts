// Private web chat history (api/routes/conversations.py). Owner-only on the server; bodies are already redacted.
import { jsonBody, requestFor } from './http.ts'
import type { Settings } from '../lib/types.ts'

export interface ConversationSummary {
  conv_id: string
  title: string
  channel: string
  message_count: number
  created_at: number
  updated_at: number
}

export interface StoredMessage {
  request_id: string
  role: 'user' | 'assistant'
  content: string
  meta: Record<string, any>
  created_at: number
}

const path = (id: string) => `/conversations/${encodeURIComponent(id)}`

export function listConversations(type: string, settings: Settings, params: { q?: string; limit?: number; before?: number } = {}, signal?: AbortSignal) {
  const query = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== undefined && v !== '').map(([k, v]) => [k, String(v)]))
  return requestFor<{ items: ConversationSummary[]; has_more: boolean }>(type, settings, `/conversations?${query}`, { signal })
}

export const getConversation = (type: string, settings: Settings, id: string) =>
  requestFor<ConversationSummary & { messages: StoredMessage[] }>(type, settings, path(id))

export const renameConversation = (type: string, settings: Settings, id: string, title: string) =>
  requestFor(type, settings, path(id), { ...jsonBody({ title }), method: 'PATCH' })

export const deleteConversation = (type: string, settings: Settings, id: string) =>
  requestFor(type, settings, path(id), { method: 'DELETE' })
