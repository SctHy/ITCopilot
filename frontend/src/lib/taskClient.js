// All shared-task HTTP paths live here; UI state and approval cards only call this port.
import { reconcileApproval, releaseApproval, withdrawApproval, requestTasks, requestTaskInbox, requestTaskHistory, continueTask, uploadTaskFile, downloadTaskFile } from './backends.js'

export function createTaskClient(settings) {
  const snapshot = { ...settings, endpoints: { ...settings.endpoints } }
  const request = (id = '', suffix = '', payload) => requestTasks(snapshot.backend, snapshot, id, suffix, payload)
  return {
    history: params => requestTaskHistory(snapshot.backend, snapshot, params),
    linked: id => request(id, '/links'),
    continue: (id, payload) => continueTask(snapshot.backend, snapshot, id, payload),
    list: params => requestTaskInbox(snapshot.backend, snapshot, params), get: id => request(id, '?event_limit=50'), create: payload => request('', '', payload),
    directory: id => request(id, '/directory'),
    events: (id, before) => request(id, '/events?' + new URLSearchParams({ before, limit: 50 })),
    upload: (id, file, key) => uploadTaskFile(snapshot.backend, snapshot, id, file, key),
    download: (id, fileId) => downloadTaskFile(snapshot.backend, snapshot, id, fileId),
    workspace: id => request(id, '/workspace'),
    capability: (id, action, payload) => request(id, '/' + action, payload),
    post: (id, payload) => request(id, '/messages', payload),
    decide: (id, approval, decision) => request(id, `/approvals/${encodeURIComponent(approval.approval_id)}`,
      { decision, payload_hash: approval.payload_hash }),
    reconcile: approval => reconcileApproval(snapshot.backend, snapshot, approval.approval_id),
    withdraw: approval => withdrawApproval(snapshot.backend, snapshot, approval.approval_id),
    release: approval => releaseApproval(snapshot.backend, snapshot, approval.approval_id),
    retry: (id, run) => request(id, `/runs/${encodeURIComponent(run)}/retry`, {}),
    stop: id => request(id, '/stop', {})
  }
}
