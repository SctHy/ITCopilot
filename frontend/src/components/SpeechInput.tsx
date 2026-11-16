import { useEffect, useRef, useState } from 'react'
import { MicIcon, SquareIcon, XIcon, LoaderCircleIcon } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { recordAudio, type Recording } from '../audio/recorder.ts'
import { requestFor } from '../api/http.ts'
import type { Settings } from '../lib/types.ts'

type Operation = { controller: AbortController; recording?: Recording; timer?: ReturnType<typeof setInterval>; stopping?: boolean; audio?: Blob }
type Phase = 'idle' | 'starting' | 'recording' | 'transcribing' | 'failed'
const guidance = '报障可说：故障现象、发生时间、已尝试的方法。申请请说清系统、权限和期限；发送前核对系统名、账号和数字。'

export function SpeechInput({ settings, disabled, onText, onBusy, value, onChange }: {
  settings: Settings; disabled: boolean; onText?: (text: string) => void; onBusy: (busy: boolean) => void
  value?: string; onChange?: (value: string) => void
}) {
  const [phase, setPhase] = useState<Phase>('idle')
  const [seconds, setSeconds] = useState(0)
  const [level, setLevel] = useState(0)
  const [quiet, setQuiet] = useState(false)
  const [error, setError] = useState('')
  const [undo, setUndo] = useState<{ before: string; after: string } | null>(null)
  const active = useRef<Operation | null>(null)
  const retryAudio = useRef<Blob | null>(null)
  const expiry = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const lastSound = useRef(0)
  const callbacks = useRef({ onText, onBusy, value, onChange, disabled })
  callbacks.current = { onText, onBusy, value, onChange, disabled }

  function release() {
    const operation = active.current
    active.current = null
    operation?.controller.abort(); operation?.recording?.cancel(); clearInterval(operation?.timer)
    retryAudio.current = null; clearTimeout(expiry.current)
    callbacks.current.onBusy(false)
  }
  function cancel() { release(); setPhase('idle'); setLevel(0); setError('') }
  useEffect(() => () => release(), [])
  useEffect(() => { if (disabled) { cancel(); setUndo(null) } }, [disabled])
  // After manual editing or sending, never restore a stale draft over the user's text.
  useEffect(() => { if (undo && value !== undo.after) setUndo(null) }, [value, undo])

  function fail(operation: Operation, err: unknown) {
    if (active.current !== operation) return
    const audio = operation.audio
    release()
    setError(err instanceof Error ? err.message : '语音识别失败，请重试')
    setPhase(audio ? 'failed' : 'idle')
    if (audio) {
      retryAudio.current = audio
      expiry.current = setTimeout(() => {
        retryAudio.current = null; setPhase('idle'); setError('录音已释放，请重新录音')
      }, 120000)
    }
  }

  async function recognize(operation: Operation) {
    try {
      const result = await requestFor<{ text: string }>(settings.backend, settings, '/speech/transcribe', {
        method: 'POST', headers: { 'Content-Type': 'audio/wav' }, body: operation.audio,
        signal: AbortSignal.any([operation.controller.signal, AbortSignal.timeout(95000)])
      })
      if (active.current !== operation || callbacks.current.disabled) return
      if (!result.text?.trim()) throw new Error('未识别到文字，请重新录音')
      const { value: current, onChange: change, onText: append } = callbacks.current
      if (change && current !== undefined) {
        const after = [current.trimEnd(), result.text.trim()].filter(Boolean).join('\n')
        if (after.length > 8000) throw new Error('加入转写后超过 8000 字，请先缩短草稿再重试')
        change(after); setUndo({ before: current, after })
      } else append?.(result.text.trim())
      cancel()
    } catch (err) { fail(operation, err) }
  }

  async function stop() {
    const operation = active.current
    if (!operation?.recording || operation.stopping) return
    operation.stopping = true
    clearInterval(operation.timer)
    setPhase('transcribing')
    try {
      operation.audio = await operation.recording.stop()
      if (active.current !== operation) return
      await recognize(operation)
    } catch (err) { fail(operation, err) }
  }

  function retry() {
    if (!retryAudio.current || active.current || disabled) return
    const operation: Operation = { controller: new AbortController(), audio: retryAudio.current }
    clearTimeout(expiry.current); retryAudio.current = null
    active.current = operation; setError(''); setPhase('transcribing'); callbacks.current.onBusy(true)
    void recognize(operation)
  }

  async function start() {
    if (active.current || disabled) return
    release()
    const operation: Operation = { controller: new AbortController() }
    active.current = operation
    setError(''); setSeconds(0); setLevel(0); setQuiet(false); setUndo(null)
    setPhase('starting'); callbacks.current.onBusy(true)
    try {
      operation.recording = await recordAudio(operation.controller.signal, () => { void stop() }, amplitude => {
        if (active.current !== operation) return
        setLevel(amplitude)
        if (amplitude > 0.015) { lastSound.current = Date.now(); setQuiet(false) }
      })
      if (active.current !== operation) { operation.recording.cancel(); return }
      setPhase('recording')
      const began = Date.now()
      lastSound.current = began
      operation.timer = setInterval(() => {
        const elapsed = Math.floor((Date.now() - began) / 1000)
        setSeconds(Math.min(120, elapsed)); setQuiet(Date.now() - lastSound.current >= 5000)
        if (elapsed >= 120) void stop()
      }, 250)
    } catch (err) {
      const reason = err instanceof DOMException && err.name === 'NotAllowedError' ? new Error('未获得麦克风权限，请在浏览器设置中允许访问') : err
      fail(operation, reason)
    }
  }

  function undoTranscript() {
    if (undo && callbacks.current.value === undo.after) callbacks.current.onChange?.(undo.before)
    setUndo(null)
  }
  const canUndo = undo && value === undo.after && !disabled

  return <div className="mb-2 flex flex-wrap items-center gap-2 px-1 text-xs text-muted-foreground">
    {phase === 'idle' ? <>
      <Button type="button" size="sm" variant="ghost" disabled={disabled} onClick={() => void start()} aria-label="开始语音输入"><MicIcon className="size-4" />语音输入</Button>
      <span>录音上传至 DeepSeek；本应用不落盘；最长 2 分钟</span>
      {canUndo && <>
        <Button type="button" size="sm" variant="ghost" onClick={undoTranscript}>撤销本次转写</Button>
        <Button type="button" size="sm" variant="ghost" onClick={() => { undoTranscript(); void start() }}>重新录音</Button>
        <span className="w-full">文字已加入草稿，请核对后发送；手动编辑后不再提供撤销。</span>
      </>}
    </> : phase === 'failed' ? <>
      <Button type="button" size="sm" variant="outline" disabled={disabled} onClick={retry}>重试识别</Button>
      <Button type="button" size="sm" variant="ghost" disabled={disabled} onClick={() => void start()}>重新录音</Button>
      <Button type="button" size="sm" variant="ghost" onClick={cancel}>丢弃录音</Button>
      <span>录音仅在浏览器内存保留 2 分钟，重试可能再次计费。</span>
    </> : <>
      <span role="status">{phase === 'starting' ? '等待麦克风…' : phase === 'recording' ? `录音中 ${seconds}s / 120s` : '正在识别…'}</span>
      {phase === 'recording' ? <>
        <meter aria-label="麦克风音量" min={0} max={1} value={level} className="h-2 w-20" />
        <Button type="button" size="sm" variant="outline" onClick={() => void stop()}><SquareIcon className="size-3" />停止并识别</Button>
        {quiet && <span role="status">近期未检测到明显声音，请检查麦克风或靠近一些。</span>}
        {seconds >= 105 && <span role="status">还剩 {120 - seconds} 秒，将自动停止并识别。</span>}
      </> : <LoaderCircleIcon className="size-4 animate-spin" />}
      <Button type="button" size="sm" variant="ghost" onClick={cancel}><XIcon className="size-4" />取消</Button>
      {phase === 'recording' && <p className="w-full">{guidance}</p>}
    </>}
    {error && <span role="alert" className="w-full text-destructive">{error}</span>}
  </div>
}
