// All shared-task HTTP paths live here; UI state and approval cards only call this port.
import { reconcileApproval, releaseApproval, withdrawApproval } from '../api/approvals.ts'
import { requestTasks, requestTaskInbox, requestTaskHistory, continueTask, uploadTaskFile, downloadTaskFile } from '../api/tasks.ts'
import type { Approval, Page, Settings, TaskEvent, TaskListItem, TaskRoom } from './types.ts'

type Params = Record<string, string | number | boolean>

/** Bound to one connection + identity snapshot; App remounts its users when either changes. */
export function createTaskClient(settings: Settings) {
  const snapshot: Settings = { ...settings, endpoints: { ...settings.endpoints } }
  const request = <T = any>(id = '', suffix = '', payload?: unknown): Promise<T> => requestTasks(snapshot.backend, snapshot, id, suffix, payload)
  return {
    history: (params: Params) => requestTaskHistory(snapshot.backend, snapshot, params),
    linked: (id: string) => request<Page<any>>(id, '/links'),
    continue: (id: string, payload: unknown) => continueTask(snapshot.backend, snapshot, id, payload),
    list: (params: Params) => requestTaskInbox(snapshot.backend, snapshot, params) as Promise<Page<TaskListItem>>,
    get: (id: string) => request<TaskRoom>(id, '?event_limit=50'),
    create: (payload: unknown) => request<TaskRoom>('', '', payload),
    directory: (id: string) => request<Page<any>>(id, '/directory'),
    events: (id: string, before: number) => request<Page<TaskEvent>>(id, '/events?' + new URLSearchParams({ before: String(before), limit: '50' })),
    upload: (id: string, file: File, key: string) => uploadTaskFile(snapshot.backend, snapshot, id, file, key),
    download: (id: string, fileId: string) => downloadTaskFile(snapshot.backend, snapshot, id, fileId),
    workspace: (id: string) => request(id, '/workspace'),
    capability: (id: string, action: string, payload: unknown) => request(id, '/' + action, payload),
    post: (id: string, payload: unknown) => request(id, '/messages', payload),
    decide: (id: string, approval: Approval, decision: string) => request(id, `/approvals/${encodeURIComponent(approval.approval_id)}`,
      { decision, payload_hash: approval.payload_hash }),
    reconcile: (approval: Approval) => reconcileApproval(snapshot.backend, snapshot, approval.approval_id),
    withdraw: (approval: Approval) => withdrawApproval(snapshot.backend, snapshot, approval.approval_id),
    release: (approval: Approval) => releaseApproval(snapshot.backend, snapshot, approval.approval_id),
    retry: (id: string, run: string) => request(id, `/runs/${encodeURIComponent(run)}/retry`, {}),
    stop: (id: string) => request(id, '/stop', {})
  }
}

export type TaskClient = ReturnType<typeof createTaskClient>
