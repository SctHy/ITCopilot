// Display formatting shared by the chat, inspector and operations views.

export function formatPercent(value: unknown): string {
  const number = Number(value || 0)
  return `${(number <= 1 ? number * 100 : number).toFixed(1)}%`
}

export function formatTime(iso: string | null | undefined): string {
  const date = new Date(iso ?? '')
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleTimeString()
}

export function formatJson(value: unknown): string {
  try {
    return JSON.stringify(value ?? {}, null, 2)
  } catch {
    return String(value ?? '')
  }
}

export function formatBody(text: unknown): string {
  return String(text ?? '').replace(/\*\*([^*]+)\*\*/g, '$1')
}

export const roleLabels: Record<string, string> = { employee: '员工', approver: '审批人', it_admin: 'IT 管理员', auditor: '审计员' }

export function formatRoles(roles: readonly string[] = []): string {
  return roles.map(role => roleLabels[role] || role).join('、')
}
