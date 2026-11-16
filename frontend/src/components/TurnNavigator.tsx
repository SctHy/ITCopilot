// Per-request navigator of a conversation (like T3 Code's thread minimap): one tick per user request on
// the left edge, the request in view highlighted, a hover card with the question and the start of the
// reply, and click to jump there.
import { useEffect, useState, type RefObject } from 'react'
import { HoverCard, HoverCardContent, HoverCardTrigger } from '@/components/ui/hover-card'
import { formatBody } from '../lib/format.ts'
import { cn } from '@/lib/utils'
import type { ChatMessage } from '../lib/types.ts'

export interface Turn { index: number; messageId: string; question: string; reply: string }

/** Group a flat message list into user requests with the reply that followed each. */
export function turnsOf(messages: ChatMessage[]): Turn[] {
  const turns: Turn[] = []
  for (const message of messages) {
    if (message.role === 'user') turns.push({ index: turns.length, messageId: message.id, question: message.content, reply: '' })
    else if (turns.length && !turns.at(-1)!.reply) turns.at(-1)!.reply = formatBody(message.content)
  }
  return turns
}

export function TurnNavigator({ turns, scrollRef }: { turns: Turn[]; scrollRef: RefObject<HTMLElement | null> }) {
  const [active, setActive] = useState(0)

  // The active request is the last one whose question has scrolled past the top third of the view.
  useEffect(() => {
    const container = scrollRef.current
    if (!container) return
    const update = () => {
      const top = container.getBoundingClientRect().top + container.clientHeight / 3
      let current = 0
      for (const turn of turns) {
        const element = container.querySelector<HTMLElement>(`[data-message-id="${turn.messageId}"]`)
        if (element && element.getBoundingClientRect().top <= top) current = turn.index
      }
      if (container.scrollTop + container.clientHeight >= container.scrollHeight - 4) current = turns.length - 1
      setActive(current)
    }
    update()
    container.addEventListener('scroll', update, { passive: true })
    return () => container.removeEventListener('scroll', update)
  }, [turns, scrollRef])

  function jump(turn: Turn) {
    const container = scrollRef.current
    const element = container?.querySelector<HTMLElement>(`[data-message-id="${turn.messageId}"]`)
    if (container && element) {
      container.scrollTo({ top: element.offsetTop - 24, behavior: 'smooth' })
    }
  }

  if (turns.length < 2) return null
  return (
    <nav aria-label="本次会话的请求" className="pointer-events-none absolute top-1/2 left-3 z-10 hidden -translate-y-1/2 lg:block">
      <ol className="pointer-events-auto flex max-h-[60vh] flex-col gap-1.5 overflow-y-auto py-1 pr-2">
        {turns.map(turn => (
          <li key={turn.messageId}>
            <HoverCard>
              <HoverCardTrigger delay={80} closeDelay={60} render={<button type="button" onClick={() => jump(turn)} aria-label={`跳到第 ${turn.index + 1} 个请求：${turn.question}`}
                    aria-current={turn.index === active ? 'step' : undefined}
                    className="group flex h-2.5 w-7 cursor-pointer items-center outline-none" />}>
                <span className={cn(
                  'h-0.5 rounded-full transition-all duration-150',
                  turn.index === active ? 'w-6 bg-foreground/70' : 'w-3.5 bg-muted-foreground/35 group-hover:w-5 group-hover:bg-foreground/50 group-focus-visible:w-5 group-focus-visible:bg-foreground/50',
                )} />
              </HoverCardTrigger>
              <HoverCardContent align="center" sideOffset={8} side="right" className="w-80 flex-col items-start p-4">
                <p className="line-clamp-2 font-semibold text-[15px] leading-snug">{turn.question}</p>
                {turn.reply
                  ? <p className="mt-2 line-clamp-3 text-muted-foreground text-sm leading-relaxed">{turn.reply}</p>
                  : <p className="mt-2 text-muted-foreground text-sm">等待回复…</p>}
              </HoverCardContent>
            </HoverCard>
          </li>
        ))}
      </ol>
    </nav>
  )
}
