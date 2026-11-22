import { Button } from '@/components/ui/button'
import { CopyIcon, CpuIcon, WrenchIcon } from 'lucide-react'
import { ApprovalCard } from './ApprovalCard.tsx'
import { Collapse, Hint, Mono, StatusChip } from './ui.tsx'
import { formatBody, formatJson } from '../lib/format.ts'
import type { ApprovalDecision, ApprovalItem, ChatMessage as Message, Identity } from '../lib/types.ts'

export type ApprovalHandler = (target: ApprovalItem, decision?: ApprovalDecision, note?: string) => void

const relatedLabels: Record<string, string> = { confirm: '待你确认提交的申请', withdraw: '可撤回的申请', release: '可提前归还的权限' }

export function BotAvatar({ className = '' }: { className?: string }) {
  return (
    <span className={`grid size-6 shrink-0 place-items-center rounded-md border border-border bg-card text-foreground ${className}`}>
      <CpuIcon className="size-3.5" />
    </span>
  )
}

export function ChatMessage({ item, identity, backendLabel, userInitial, onApproval, onCopy }: {
  item: Message; identity: Identity | null; backendLabel: string; userInitial: string
  onApproval: ApprovalHandler; onCopy: (content: string) => void
}) {
  if (item.role === 'user') {
    return (
      <article className="fade-up flex justify-end" data-user={userInitial}>
        <div className="max-w-[85%] rounded-xl bg-accent px-4 py-2.5 text-[15px] leading-7">
          <p className="prose-reply">{item.content}</p>
        </div>
      </article>
    )
  }
  const toolCount = item.trace?.toolCalls?.length || item.trace?.toolsUsed?.length || 0
  return (
    <article className="fade-up group min-w-0">
      <div>
        <div className="mb-1.5 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          <span>{backendLabel.toLowerCase()} agent</span>
          {item.meta && !item.error && <span className="truncate">· {item.meta}</span>}
          {item.error && <StatusChip tone="danger">请求失败</StatusChip>}
        </div>
        <div className={`text-[15px] leading-7 ${item.error ? 'rounded-lg border border-border bg-card px-3.5 py-2.5 text-destructive-foreground' : ''}`}>
          <p className="prose-reply">{formatBody(item.content)}</p>
          {item.approvalId && (
            <ApprovalCard approval={item.approval} busy={item.approvalBusy} error={item.approvalError} identity={identity}
              onAction={(decision, note) => onApproval(item as ApprovalItem, decision, note)} />
          )}
          {(item.relatedApprovals || []).map(related => (
            <div key={related.approvalId} className="mt-3">
              <p className="text-[13px] text-muted-foreground">{relatedLabels[related.kind] || '可提前归还的权限'}：{related.summary}</p>
              <ApprovalCard approval={related.approval} busy={related.approvalBusy} error={related.approvalError} identity={identity}
                onAction={(decision, note) => onApproval(related, decision, note)} />
            </div>
          ))}
          {toolCount > 0 && (
            <Collapse className="mt-3" triggerClassName="" panelClassName="grid gap-2 pt-2" trigger={<><WrenchIcon className="size-3" />{toolCount} 个工具调用</>}>
                  {(item.trace?.toolCalls || []).map((call, index) => (
                    <div key={`${item.id}-${index}`} className="rounded-md border border-border p-2.5">
                      <div className="mb-2 flex items-center justify-between gap-2">
                        <code className="text-[13px] font-semibold">{call.tool_name || 'unknown_tool'}</code>
                        <StatusChip tone={call.success ? 'success' : 'danger'}>{call.success ? '成功' : '失败'}</StatusChip>
                      </div>
                      <Mono>{formatJson(call.input || {})}</Mono>
                      {!call.success && call.error && <p className="mt-2 text-[13px] text-destructive-foreground">{call.error}</p>}
                    </div>
                  ))}
                  {!item.trace?.toolCalls?.length && <p className="text-[13px] text-muted-foreground">已调用：{item.trace?.toolsUsed?.join(' · ')}</p>}
                </Collapse>
          )}
        </div>
        <div className="flex items-start gap-1 opacity-60 transition-opacity group-hover:opacity-100">
          {!item.error && (
            <Hint label={<>复制回复</>}><Button type="button" size="icon" variant="ghost" aria-label="复制回复" className="mt-2" onClick={() => onCopy(item.content)}>
                <CopyIcon className="size-3.5" />
              </Button></Hint>
          )}
        </div>
      </div>
    </article>
  )
}
