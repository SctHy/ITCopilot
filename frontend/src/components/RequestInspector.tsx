import type { ReactNode } from 'react'
import { Spinner } from '@/components/ui/spinner'
import { BookOpenIcon, MessageSquareIcon, RouteIcon, SearchIcon } from 'lucide-react'
import { formatJson, formatPercent } from '../lib/format.ts'
import { Collapse, Mono } from './ui.tsx'

function Step({ icon, title, text, done, active }: { icon: ReactNode; title: string; text: string; done: boolean; active: boolean }) {
  return (
    <li className="relative flex gap-2.5 pb-3.5 last:pb-0">
      <span className="absolute top-6 bottom-0 left-[9.5px] w-px bg-border" aria-hidden="true" />
      <span className={`relative z-10 grid size-5 shrink-0 place-items-center rounded-md border bg-card [&>svg]:size-3 ${
        done ? 'border-border text-foreground' : active ? 'border-ring text-foreground' : 'border-border text-muted'}`}>
        {icon}
      </span>
      <div className="min-w-0">
        <p className="text-[13px] leading-5">{title}{done && <span className="ml-1.5 inline-block size-1.5 rounded-full bg-success align-middle" />}</p>
        <p className="text-xs break-words text-muted-foreground">{text}</p>
      </div>
    </li>
  )
}

function Metric({ label, value, tone = '' }: { label: string; value: ReactNode; tone?: string }) {
  return (
    <div className="py-2">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className={`mt-0.5 text-[15px] tabular-nums ${tone}`}>{value}</p>
    </div>
  )
}

export function RequestInspector({ lastResponse, lastTrace, busy }: {
  lastResponse: Record<string, any> | null; lastTrace: Record<string, any> | null; busy: boolean
}) {
  const done = !!lastResponse && !busy
  const calls: any[] = lastTrace?.trace?.toolCalls || []
  return (
    <div className="flex flex-col gap-6">
      <section>
        <div className="mb-1 flex items-center justify-between">
          <h3 className="text-[13px] font-medium">请求追踪</h3>
          <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
            {busy ? <Spinner className="size-3" /> : <span className={`size-1.5 rounded-full ${done ? 'bg-success' : 'bg-muted'}`} />}
            {busy ? '处理中' : lastResponse ? '已完成' : '待开始'}
          </span>
        </div>
        <p className="mb-4 text-[13px] text-muted-foreground">{busy ? '正在等待服务返回处理结果。' : lastResponse ? '本次请求的处理路径与路由结果。' : '发送一条消息，查看 Agent 的处理过程。'}</p>
        <ol>
          <Step icon={<SearchIcon />} title="意图识别" text={lastResponse?.intent || '理解员工的 IT 问题'} done={done} active={busy} />
          <Step icon={<BookOpenIcon />} title="知识检索" text={lastResponse ? lastResponse.knowledgeUsed ? '已使用知识库' : '未使用知识库' : '查找相关业务知识'} done={done} active={busy} />
          <Step icon={<RouteIcon />} title="工具调用" text={lastResponse ? lastResponse.toolsUsed?.length ? lastResponse.toolsUsed.join(' · ') : '未调用工具' : '按需调用工具'} done={done} active={busy} />
          <Step icon={<MessageSquareIcon />} title="生成回复" text={busy ? '等待回复' : lastResponse ? lastResponse.escalated ? '已转人工处理' : '回复已生成' : '等待新的请求'} done={done} active={busy} />
        </ol>
        <div className="mt-4 grid grid-cols-2 gap-2">
          <Metric label="响应耗时" value={lastResponse ? <>{Math.round(Number(lastResponse.latencyMs) || 0).toLocaleString()}<small className="ml-0.5 text-[13px] font-normal text-muted-foreground">ms</small></> : '—'} />
          <Metric label="意图置信度" value={lastResponse ? formatPercent(lastResponse.intentConfidence) : '—'} />
        </div>
        {lastTrace?.trace && (
          <Collapse className="mt-3" triggerClassName="" panelClassName="grid gap-2 pt-2" trigger={<><span className="flex items-center gap-2 text-xs text-muted-foreground">{calls.length} 个工具调用</span></>}>
                {calls.map((call, index) => (
                  <div key={index} className="rounded-md border border-border p-3">
                    <div className="mb-2 flex items-center justify-between text-[13px]">
                      <code className="font-semibold">{call.tool_name}</code>
                      <span className="text-muted-foreground">{call.latency_ms ?? '—'} ms</span>
                    </div>
                    <Mono>{formatJson(call.input || {})}</Mono>
                  </div>
                ))}
                {!calls.length && <p className="text-[13px] text-muted-foreground">{lastTrace.trace.toolsUsed?.length ? lastTrace.trace.toolsUsed.join(' · ') : '本次没有记录到工具输入。'}</p>}
              </Collapse>
        )}
      </section>
    </div>
  )
}
