import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { ThumbsDownIcon, ThumbsUpIcon } from 'lucide-react'
import { jsonBody, requestFor } from '../api/http.ts'
import type { Settings } from '../lib/types.ts'
import { SelectInput, TextAreaInput } from './ui.tsx'

const reasons = [
  { id: 'wrong_answer', label: '答案有误' }, { id: 'not_done', label: '事情没办完' }, { id: 'false_claim', label: '声称做了但未完成' },
  { id: 'slow', label: '回复慢' }, { id: 'wrong_language', label: '语言不合适' }, { id: 'unsafe', label: '处理不安全' }, { id: 'other', label: '其他' }
]

export function FeedbackButtons({ settings, path }: { settings: Settings; path: string }) {
  const [busy, setBusy] = useState(false)
  const [editing, setEditing] = useState(false)
  const [rating, setRating] = useState(0)
  const [reason, setReason] = useState('wrong_answer')
  const [comment, setComment] = useState('')
  const [error, setError] = useState('')
  const [saved, setSaved] = useState(false)

  async function submit(value: number) {
    setBusy(true); setError(''); setSaved(false)
    try {
      await requestFor(settings.backend, settings, path, jsonBody({ rating: value, reason: value === 1 ? 'other' : reason, comment }))
      setRating(value); setEditing(false); setSaved(true)
    } catch (cause) { setError(String(cause)) }
    finally { setBusy(false) }
  }

  return (
    <div className="mt-2 flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-1">
        <Button type="button" size="icon" variant={rating === 1 ? 'secondary' : 'ghost'} aria-label="有帮助" aria-pressed={rating === 1} disabled={busy} onClick={() => submit(1)}>
          <ThumbsUpIcon className={`size-3.5 ${rating === 1 ? 'text-success' : ''}`} />
        </Button>
        <Button type="button" size="icon" variant={rating === -1 || editing ? 'secondary' : 'ghost'} aria-label="需改进" aria-pressed={rating === -1} disabled={busy} onClick={() => setEditing(!editing)}>
          <ThumbsDownIcon className={`size-3.5 ${rating === -1 ? 'text-destructive-foreground' : ''}`} />
        </Button>
        {saved && <small role="status" className="text-[13px] text-success-foreground">反馈已保存</small>}
        {error && <small role="alert" className="text-[13px] text-destructive-foreground">{error}</small>}
      </div>
      {editing && (
        <form className="fade-up grid max-w-md gap-3 rounded-md border border-border bg-card p-3" onSubmit={event => { event.preventDefault(); submit(-1) }}>
          <SelectInput label="原因" value={reason} onChange={setReason} options={reasons} />
          <TextAreaInput label="备注（选填）" value={comment} onChange={setComment} maxLength={500} rows={2} />
          <div className="flex gap-2">
            <Button size="sm" type="submit" disabled={busy}>提交反馈</Button>
            <Button type="button" size="sm" variant="ghost" onClick={() => setEditing(false)}>取消</Button>
          </div>
        </form>
      )}
    </div>
  )
}
