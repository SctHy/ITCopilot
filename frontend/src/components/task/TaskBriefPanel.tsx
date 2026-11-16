import { Button } from '@/components/ui/button'
import type { TaskBrief, TaskRoom } from '../../lib/types.ts'
import { PanelItem } from './TaskPanels.tsx'

const categories = [['reported', '用户原话 · 含问题与假设'], ['answered', '已回答 · 依据用户答复'], ['hypotheses', '推测 · 尚未核实'], ['questions', '仍待确认']] as const

export function TaskBriefPanel({ brief, room }: { brief?: TaskBrief | null; room: TaskRoom }) {
  const latestReply = room.events.findLast(event => event.kind === 'assistant')
  const unprocessed = brief?.through_seq !== undefined && room.events.some(event => event.seq > brief.through_seq! && ['requester', 'collaborator', 'approver', 'todo_answer'].includes(event.kind))
  function showSource(id: string) {
    const element = document.getElementById(`task-event-${id}`)
    element?.scrollIntoView({ block: 'center', behavior: 'smooth' })
    element?.focus({ preventScroll: true })
  }
  return <PanelItem id="brief" title="讨论摘要">
    {!brief ? <p className="text-[13px] text-muted-foreground">Agent 后续回复时会整理已提供的信息、推测与待确认问题，并保留消息来源。</p> : <>
      <p className="text-xs text-muted-foreground">自动整理于 {new Date(brief.updated_at * 1000).toLocaleString('zh-CN')}。用户陈述不等于系统核验结果。</p>
      {brief.origin && <section aria-label="原始需求与限定" className="rounded-md border border-border p-2.5">
        <h4 className="text-sm font-medium">原始需求与限定 · 原文保留</h4>
        <p className="mt-1 whitespace-pre-wrap break-words text-sm">{brief.origin.quote}</p>
      </section>}
      {(unprocessed || latestReply && latestReply.id !== brief.event_id) && <p className="text-xs text-warning-foreground">有新讨论尚未纳入摘要，请结合最新消息查看。</p>}
      {categories.map(([key, label]) => <section key={key} aria-label={label} className="space-y-2">
        <h4 className="text-sm font-medium text-foreground">{label}</h4>
        {!brief[key]?.length ? <p className="text-xs text-muted-foreground">暂未整理出相关条目</p> : <ul className="space-y-2">
          {brief[key]!.map((item, index) => <li key={index} className="rounded-md bg-muted/50 p-2.5">
            <p className="whitespace-pre-wrap break-words text-sm text-foreground">{item.text}</p>
            <details className="mt-1 text-xs text-muted-foreground">
              <summary className="cursor-pointer">查看来源 · {[...new Set(item.sources.map(source => source.actor_name || '事项消息'))].join('、')}</summary>
              {item.sources.map((source, sourceIndex) => <div key={sourceIndex} className="mt-2 border-l-2 border-border pl-2">
                <p>{source.actor_name || '事项消息'}{source.created_at ? ` · ${new Date(source.created_at * 1000).toLocaleString('zh-CN')}` : ''}</p>
                <blockquote className="mt-1 whitespace-pre-wrap break-words">{source.quote}</blockquote>
                {room.events.some(event => event.id === source.event_id && ['requester', 'approver', 'collaborator', 'assistant'].includes(event.kind)) &&
                  <Button type="button" size="sm" variant="ghost" onClick={() => showSource(source.event_id)}>定位原消息</Button>}
              </div>)}
            </details>
          </li>)}
        </ul>}
      </section>)}
    </>}
  </PanelItem>
}
