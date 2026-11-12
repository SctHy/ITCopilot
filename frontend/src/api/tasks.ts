// Shared rooms use server-side transcripts and durable request keys, not private chat memory.
import { authHeaders, backendMeta, jsonBody, requestFor } from './http.ts'
import type { Settings } from '../lib/types.ts'

type Params = Record<string, string | number | boolean>
const query = (params: Params) => new URLSearchParams(Object.entries(params).map(([k, v]) => [k, String(v)]))

export const requestTaskInbox = (type: string, settings: Settings, params: Params = {}) =>
  requestFor(type, settings, '/task-inbox?' + query(params))

export const requestTaskHistory = (type: string, settings: Settings, params: Params = {}) =>
  requestFor(type, settings, '/task-history?' + query(params))

export const continueTask = (type: string, settings: Settings, id: string, payload: unknown) =>
  requestFor(type, settings, `/task-history/${encodeURIComponent(id)}/continue`, jsonBody(payload))

export function requestTasks(type: string, settings: Settings, taskId = '', suffix = '', payload: unknown = undefined) {
  const path = `/tasks${taskId ? `/${encodeURIComponent(taskId)}` : ''}${suffix}`
  return requestFor(type, settings, path, payload === undefined ? {} : jsonBody(payload))
}

export function uploadTaskFile(type: string, settings: Settings, id: string, file: File, requestId: string) {
  const path = `/tasks/${encodeURIComponent(id)}/files?` + new URLSearchParams({ name: file.name, client_request_id: requestId })
  return requestFor(type, settings, path, { method: 'POST', body: file, headers: { 'Content-Type': 'application/octet-stream' } })
}

export async function downloadTaskFile(type: string, settings: Settings, id: string, fileId: string): Promise<Blob> {
  const response = await fetch(`${backendMeta(type, settings).baseUrl}/tasks/${encodeURIComponent(id)}/files/${encodeURIComponent(fileId)}`, {
    headers: authHeaders(settings), signal: AbortSignal.timeout(30000)
  })
  if (!response.ok) throw new Error('下载失败，请检查事项访问权限')
  return response.blob()
}
