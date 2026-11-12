// Knowledge base: retrieval, ingestion and document management.
import { jsonBody, requestFor } from './http.ts'
import type { Settings } from '../lib/types.ts'

export const requestKnowledgeStats = (type: string, settings: Settings) => requestFor(type, settings, '/knowledge/stats')

export async function requestSearch(type: string, settings: Settings, query: string, topK = 5) {
  const params = new URLSearchParams({ query, top_k: String(topK) })
  const data = await requestFor(type, settings, `/search?${params}`, { method: 'POST' })
  if (data?.error) throw new Error(data.error)
  return { results: Array.isArray(data) ? data : (data?.results || data?.items || []) }
}

export const addKnowledge = (type: string, settings: Settings, documents: { title: string; content: string }[]) =>
  requestFor(type, settings, '/knowledge/add', jsonBody({ documents }))

export const listKnowledgeDocuments = (type: string, settings: Settings) => requestFor(type, settings, '/knowledge/documents')

export const deleteKnowledgeDocument = (type: string, settings: Settings, documentId: string) =>
  requestFor(type, settings, `/knowledge/documents/${encodeURIComponent(documentId)}`, { method: 'DELETE' })

export function uploadKnowledge(type: string, settings: Settings, file: Blob) {
  const form = new FormData()
  form.append('file', file)
  return requestFor(type, settings, '/knowledge/upload', { method: 'POST', body: form })
}
