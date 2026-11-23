// Shapes the UI reads from the IT-Copilot API. Only fields the frontend uses are listed;
// the server remains the source of truth and may send more.

export type Role = 'employee' | 'approver' | 'it_admin' | 'auditor'

export interface Settings {
  backend: string
  endpoints: Record<string, string>
  conversationId: string
  accessToken?: string
}

export interface Identity {
  subject?: string
  username: string
  employee_id?: string
  department: string
  roles: Role[]
}

export type ApprovalStatus = 'draft' | 'pending' | 'approved' | 'rejected' | 'withdrawn' | 'expired' | (string & {})

export interface ApprovalActionRequest {
  tool_name: string
  arguments?: Record<string, unknown>
}

export interface ApprovalVote {
  actor: string
  actor_name?: string
  decision: 'approved' | 'rejected' | (string & {})
}

/** One executed (or compensating) action or a scheduled revocation. */
export interface ApprovalStep {
  version?: number
  operation_id: string
  action?: string
  status: string
  adapter?: string
  drifted?: boolean
  preexisting?: boolean
  covered_by?: string
  reconciled?: boolean
  error?: string
  not_before?: string | null
}

export interface Approval {
  approval_id: string
  status: ApprovalStatus
  requester?: string
  requester_name?: string
  policy_version?: string
  approvals_required?: number
  expires_at?: string
  /** Drafted by the Agent; only enters the approver queue once the requester confirms it (ADR 0020). */
  awaiting_confirmation?: boolean
  confirmed_at?: string | null
  execution?: string
  lifecycle?: string
  payload_hash?: string
  action_requests?: ApprovalActionRequest[]
  decisions?: ApprovalVote[]
  assigned_reviewers?: string[]
  results?: ApprovalStep[]
  audit?: { event: string, actor: string, detail?: { reason?: string } }[]
  revocations?: ApprovalStep[]
  room_can_write?: boolean
  conflicts?: { approval_id: string, status: string, requester: string, conv_channel: string }[]
}

export interface ReviewDecision {
  resolution: 'retry' | 'accept_external' | 'mark_resolved'
  operation_id: string
  version: number
  reason: string
}

export type ApprovalDecision = ReviewDecision | 'approved' | 'rejected' | 'clarify' | 'reconcile' | 'confirm' | 'withdraw' | 'release' | 'resolve'

/** An approval card bound to the connection that produced it. */
export interface ApprovalItem {
  approvalId: string
  approval: Approval | null
  approvalBackend: string
  approvalSettings: Settings
  approvalBusy?: boolean
  approvalError?: string
}

export interface ToolCall {
  tool_name?: string
  input?: unknown
  success?: boolean
  error?: string
}

export interface MessageTrace {
  toolCalls?: ToolCall[]
  toolsUsed?: string[]
}

export interface RelatedApproval extends ApprovalItem {
  kind: 'confirm' | 'withdraw' | 'release' | 'resolve' | (string & {})
  summary: string
}

export interface ChatMessage {
  requestId?: string
  id: string
  role: 'user' | 'assistant'
  content: string
  meta?: string
  error?: boolean
  trace?: MessageTrace | null
  approvalId?: string
  approval?: Approval | null
  approvalBackend?: string
  approvalSettings?: Settings
  approvalBusy?: boolean
  approvalError?: string
  relatedApprovals?: RelatedApproval[]
}
