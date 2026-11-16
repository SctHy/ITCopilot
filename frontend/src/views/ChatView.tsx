// Private chat page. The session lives in App so the conversation (and an in-flight reply)
// survives switching to another page and back; this component only renders it.
// Layout follows T3 Code: a ~760px reading column, the composer pinned to the bottom,
// and run details in a floating card on the right.
import { useEffect, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Kbd, KbdGroup } from '@/components/ui/kbd'
import { Sheet, SheetDescription, SheetHeader, SheetContent, SheetTitle } from '@/components/ui/sheet'
import { ArrowUpIcon, GlobeIcon, HistoryIcon, KeyRoundIcon, RouteIcon, ShieldIcon, TicketIcon } from 'lucide-react'
import { useStore } from '../hooks/useStore.ts'
import { SpeechInput } from '../components/SpeechInput.tsx'
import { ChatMessage } from '../components/ChatMessage.tsx'
import { RequestInspector } from '../components/RequestInspector.tsx'
import { TaskHistory } from '../components/TaskHistory.tsx'
import { Collapse } from '../components/ui.tsx'
import { TurnNavigator, turnsOf } from '../components/TurnNavigator.tsx'
import { applyApprovalAction } from '../lib/approvalDecision.ts'
import type { ChatSession } from '../lib/chatSession.ts'
import type { TaskClient } from '../lib/taskClient.ts'
import type { Identity, MonitorData, Settings } from '../lib/types.ts'

const prompts = [
  { label: 'VPN 连不上', hint: '检查网络、DNS 和客户端状态', text: 'VPN 连不上，错误码 VPN-691，应该怎么排查？', icon: <GlobeIcon /> },
  { label: '重置密码', hint: '走身份平台的安全重置流程', text: '我忘记了企业密码，如何安全重置？', icon: <KeyRoundIcon /> },
  { label: '申请权限', hint: '起草最小权限申请，审批后生效', text: '请帮我申请 GitLab 项目只读权限。', icon: <ShieldIcon /> },
  { label: '查询工单', hint: '查看我提交过的工单和进度', text: '查询我的工单', icon: <TicketIcon /> }
]

export function ChatView({ session, settings, identity, backendLabel, monitorData, taskHistoryClient, onOpenTask, onRefreshMonitor, onToast,
  panelOpen, panelDrawer, onPanelDrawerChange }: {
  session: ChatSession; settings: Settings; identity: Identity | null; backendLabel: string; monitorData: MonitorData
  taskHistoryClient: TaskClient; onOpenTask: (id: string) => void; onRefreshMonitor: () => void; onToast: (message: string) => void
  panelOpen: boolean; panelDrawer: boolean; onPanelDrawerChange: (open: boolean) => void
}) {
  const state = useStore(session.state)
  const listRef = useRef<HTMLDivElement | null>(null)
  const composerRef = useRef<HTMLTextAreaElement | null>(null)
  const [speechBusy, setSpeechBusy] = useState(false)
  const [historyOpen, setHistoryOpen] = useState(false)
  const hasMessages = state.messages.length > 0
  const userInitial = (identity?.username || '?').slice(0, 1).toUpperCase()
  const lastContent = state.messages.at(-1)?.content.length ?? 0
  const showPanel = panelOpen && hasMessages

  // Follow the conversation: scroll when a message is added, while a reply streams, and when it finishes.
  useEffect(() => {
    const list = listRef.current
    if (list) list.scrollTo({ top: list.scrollHeight, behavior: 'smooth' })
  }, [state.messages.length, state.busy, lastContent])

  function usePrompt(text: string) {
    state.draft = text
    requestAnimationFrame(() => composerRef.current?.focus())
  }

  async function copyMessage(content: string) {
    try { await navigator.clipboard.writeText(content); onToast('回复已复制') }
    catch { onToast('复制失败，请手动选择回复文字') }
  }

  const inspector = (
    <RequestInspector lastResponse={state.lastResponse} lastTrace={state.lastTrace} busy={state.busy} monitorData={monitorData} onRefresh={onRefreshMonitor} />
  )

  return (
    <section className="relative flex h-full min-h-0">
      <div className={`flex min-w-0 flex-1 flex-col ${showPanel ? 'xl:pr-[340px]' : ''}`}>
        <div ref={listRef} className="min-h-0 flex-1 overflow-y-auto" role="log" aria-label="IT 服务台对话" aria-live="polite" aria-busy={state.busy}>
          <div className="mx-auto flex min-h-full w-full max-w-[760px] flex-col px-5 sm:px-8">
            {!hasMessages ? (
              <div className="flex flex-1 flex-col justify-end gap-6 pt-10 pb-6">
                <div>
                  <h1 className="text-2xl font-semibold tracking-tight">需要处理什么 IT 问题？</h1>
                  <p className="mt-2 text-[15px] leading-relaxed text-muted-foreground">描述问题或申请。Agent 会检索知识、诊断并起草变更；真正的变更需人工审批后由系统执行，到期自动回收。</p>
                </div>
                <div className="grid gap-2.5 sm:grid-cols-2">
                  {prompts.map(prompt => (
                    <button key={prompt.label} type="button" onClick={() => usePrompt(prompt.text)}
                      className="flex items-start gap-3 rounded-xl border border-border bg-card px-4 py-3.5 text-left transition-colors hover:border-ring hover:bg-muted focus-visible:ring-1 focus-visible:ring-ring focus-visible:outline-none">
                      <span className="mt-0.5 text-muted-foreground [&>svg]:size-4">{prompt.icon}</span>
                      <span className="min-w-0">
                        <span className="block text-sm font-medium">{prompt.label}</span>
                        <span className="mt-0.5 block text-[13px] text-muted-foreground">{prompt.hint}</span>
                      </span>
                    </button>
                  ))}
                </div>
                {identity && (
                  <div className="rounded-xl border border-border bg-card">
                    <Collapse open={historyOpen} onOpenChange={setHistoryOpen} triggerClassName="h-11 w-full justify-between rounded-xl px-4 font-normal text-foreground"
                      trigger={<span className="flex items-center gap-3 text-sm"><HistoryIcon className="size-4 text-muted-foreground" />查找并继续历史事项</span>}
                      panelClassName="border-t px-4 py-3">
                      <TaskHistory client={taskHistoryClient} active={historyOpen} onOpen={onOpenTask} />
                    </Collapse>
                  </div>
                )}
              </div>
            ) : (
              <div className="flex flex-col gap-7 py-8">
                {state.messages.map(item => (
                  <div key={item.id} data-message-id={item.id}><ChatMessage item={item} identity={identity} backendLabel={backendLabel} userInitial={userInitial}
                    onApproval={(target, decision, note) => applyApprovalAction(target, settings, decision, note)}
                    onCopy={copyMessage} onOpenTask={onOpenTask} /></div>
                ))}
                {state.busy && (
                  <div className="fade-up flex items-center gap-2.5 text-[13px] text-muted-foreground" role="status">
                    <span className="flex gap-1">
                      <i className="typing-dot size-1.5 rounded-full bg-muted" />
                      <i className="typing-dot size-1.5 rounded-full bg-muted" />
                      <i className="typing-dot size-1.5 rounded-full bg-muted" />
                    </span>
                    {state.streamStatus || '正在处理问题'}
                  </div>
                )}
              </div>
            )}
          </div>
        </div>

        <div className="mx-auto w-full max-w-[760px] px-5 pb-4 sm:px-8">
          <form onSubmit={event => { event.preventDefault(); if (!speechBusy) session.send() }}
            className="rounded-2xl border bg-card px-3 pt-3 pb-2.5 shadow-xs/5 transition-colors focus-within:border-ring/60">
            <SpeechInput key={JSON.stringify([settings.backend, settings.endpoints, settings.accessToken, settings.conversationId])}
              settings={settings} disabled={state.busy} onBusy={setSpeechBusy}
              value={state.draft} onChange={text => { state.draft = text; composerRef.current?.focus() }} />
            <textarea aria-label="员工 IT 问题" value={state.draft} onChange={event => { state.draft = event.target.value }} rows={2}
              placeholder="描述你遇到的 IT 问题，或想办理的申请…" ref={composerRef}
              className="field-sizing-content max-h-60 min-h-12 w-full resize-none bg-transparent px-1.5 text-[15px] leading-relaxed outline-none placeholder:text-muted-foreground"
              onKeyDown={event => { if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') { event.preventDefault(); if (!speechBusy) session.send() } }} />
            <div className="mt-1 flex items-center justify-between gap-2 px-1">
              <span className="flex items-center gap-1.5 text-[13px] text-muted-foreground"><RouteIcon className="size-3.5" />自动路由</span>
              <div className="flex items-center gap-2.5">
                <span className="hidden items-center gap-1.5 text-xs text-muted-foreground sm:flex">
                  <KbdGroup><Kbd>⌘</Kbd><Kbd>Enter</Kbd></KbdGroup>发送
                </span>
                <Button size="icon" type="submit" className="size-9 rounded-full" aria-label={state.busy ? '处理中' : '发送'}
                  aria-busy={state.busy} disabled={state.busy || speechBusy || !state.draft.trim()}>
                  <ArrowUpIcon className="size-4" />
                </Button>
              </div>
            </div>
          </form>
          <p className="mt-2 text-center text-xs text-muted-foreground/70">AI 生成的内容可能不准确，请结合业务规则核实。</p>
        </div>
      </div>

      {hasMessages && <TurnNavigator turns={turnsOf(state.messages)} scrollRef={listRef} />}

      {showPanel && (
        <aside className="absolute top-4 right-4 bottom-4 hidden w-[320px] overflow-y-auto rounded-xl border bg-card p-4 shadow-xs/5 xl:block" aria-label="请求追踪与运行状态">
          {inspector}
        </aside>
      )}

      <Sheet open={panelDrawer} onOpenChange={onPanelDrawerChange}>
        <SheetContent side="right" className="w-[min(92vw,380px)]">
          <SheetHeader><SheetTitle>运行详情</SheetTitle><SheetDescription className="sr-only">查看当前请求的工具调用和运行状态</SheetDescription></SheetHeader>
          <div className="overflow-y-auto p-4">{inspector}</div>
        </SheetContent>
      </Sheet>
    </section>
  )
}
