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
  operation_id: string
  action?: string
  status: string
  adapter?: string
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
  revocations?: ApprovalStep[]
  room_can_write?: boolean
  conflicts?: { approval_id: string, status: string, requester: string, conv_channel: string }[]
}

export type ApprovalDecision = 'approved' | 'rejected' | 'clarify' | 'reconcile' | 'confirm' | 'withdraw' | 'release'

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
  kind: 'confirm' | 'withdraw' | 'release' | (string & {})
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
  taskReferences?: { id: string; title: string }[]
  approvalId?: string
  approval?: Approval | null
  approvalBackend?: string
  approvalSettings?: Settings
  approvalBusy?: boolean
  approvalError?: string
  relatedApprovals?: RelatedApproval[]
}

export interface MonitorSlo {
  name: string
  target: number
  burn_rate: Record<string, number>
  requests_1h: number
  bad_1h: number
}

export interface MonitorAlert {
  key: string
  severity: 'critical' | 'warning' | (string & {})
  message: string
  action?: string
}

export interface MonitorSummary {
  status: 'ok' | 'degraded' | 'critical' | 'unknown' | (string & {})
  slos: MonitorSlo[]
  alerts: MonitorAlert[]
  recent_resolved: unknown[]
  last_tick: string | null
}

export interface MonitorData {
  loaded?: boolean
  agent: { model?: string; total?: number; recent_total?: number; recent_success_rate?: number; [key: string]: unknown }
  tools: Record<string, { circuit_state: string }>
  monitor: MonitorSummary | null
  mcp: { servers: { name: string; state: string; tools: unknown[] }[] }
}

// ---- Shared tasks (see workflow/tasks.py `get` / `list`) ----

export type TaskStatus = 'open' | 'resolved' | 'stopped'

export interface TaskEvent {
  id: string
  seq: number
  kind: string
  actor: string
  actor_name: string
  text: string
  created_at: number
  data?: { approval_id?: string; [key: string]: unknown }
}

export interface TaskRun { id: string; seq: number; status: 'queued' | 'running' | 'done' | 'failed' | 'held' | (string & {}); attempts: number; error?: string | null }

export interface TaskRoom {
  id: string
  title: string
  requester: string
  requester_name: string
  status: TaskStatus
  phase: string
  can_write: boolean
  events: TaskEvent[]
  has_more_events: boolean
  approvals?: Approval[]
  runs?: TaskRun[]
}

export interface TaskListItem { id: string; title: string; requester_name: string; status: TaskStatus; unread?: number }

export interface Page<T> { items: T[]; total?: number; has_more?: boolean }

export interface TaskQuery { q: string; status: string; archived: boolean; category: string; offset: number; limit: number }

// ---- Shared-task workspace (workflow/task_capabilities.py `workspace`) ----

export interface TaskMember { subject: string; role: 'collaborator' | 'observer' | 'revoked' | (string & {}); profile: { username: string } }
export interface DirectoryPerson { subject: string; username: string; department: string; roles: string[] }
export interface TaskFile { id: string; name: string; size: number }
export interface TaskTodo { id: string; question: string; assignee: string; creator: string; status: 'open' | 'answered' | 'cancelled' | (string & {}); due_at?: number | null; answer?: string }
export interface TaskReminder { id: string; text: string; due_at: number; status: 'pending' | 'fired' | 'cancelled' | (string & {}) }
export interface ProgressStep { title: string; status: 'todo' | 'working' | 'done' | (string & {}); evidence_ids: string[] }
export interface TaskProgress { version: number; goal: string; steps: ProgressStep[]; blocker?: string; next_action?: string; resolution?: string }
export interface TaskSuggestion { id: string; status: 'pending' | 'accepted' | 'dismissed' | (string & {}); base_version: number; payload: TaskProgress }
export interface AccessCheck { kind?: string; system: string; role: string }
export type GoalCheck = AccessCheck | { kind: 'device_locked'; device_id: string } | { kind: 'ticket_open'; ticket_id: string }
export interface VerificationRecord {
  id: string
  status: string
  observations: { system?: string; device_id?: string; ticket_id?: string; status: string; reason?: string; actual?: string | boolean | null; expected?: string | boolean; role?: string }[]
}
export interface TaskVerification {
  goal: { version: number; checks: GoalCheck[]; replans_used: number; replan_limit: number; latest_id?: string | null }
  history: VerificationRecord[]
  catalog: { system: string; roles: string[] }[]
  acceptable: boolean
  fresh_seconds: number
}
export interface TaskCheckpoint { run_id: string; step_key: string; created_at: number }
export interface TaskBudget { used_runs: number; run_limit: number; used_tokens: number; token_limit: number }
export interface TaskAttempt {
  id: string; run_id: string; status: string; attempt: number
  queued_at: number; started_at: number; ended_at?: number | null; error?: string | null
  tools: { tool_name: string; success: boolean }[]
}
export interface TaskNotifications {
  channels: string[]
  subscription?: { channel: string; enabled: boolean; escalate_to?: string | null } | null
  deliveries: { id: string; channel: string; status: string; attempts: number; error?: string | null }[]
}
export interface TaskWorkspaceData {
  verification?: TaskVerification | null
  checkpoints?: TaskCheckpoint[]
  progress: TaskProgress
  todos: TaskTodo[]
  reminders: TaskReminder[]
  files?: TaskFile[]
  members?: TaskMember[]
  reviewers?: string[]
  suggestions?: TaskSuggestion[]
  notifications?: TaskNotifications | null
  budget: TaskBudget
  attempts: TaskAttempt[]
}
