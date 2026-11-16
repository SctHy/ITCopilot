import { useEffect, useState } from 'react'
import { Button } from '@/components/ui/button'
import { FlagIcon, RefreshCwIcon } from 'lucide-react'
import { jsonBody, requestFor } from '../api/http.ts'
import type { Settings } from '../lib/types.ts'
import { Empty, ErrorAlert, InfoAlert, Mono, Panel, SelectInput, StatusChip, TextAreaInput, type Tone } from './ui.tsx'

const states = ['new', 'triaged', 'labeled', 'promoted', 'dismissed']
const stateTones: Record<string, Tone> = { new: 'accent', triaged: 'warning', labeled: 'success', promoted: 'success', dismissed: 'default' }
const causes = ['intent_wrong', 'slot_missing_or_default', 'retrieval_miss', 'retrieval_irrelevant', 'tool_not_called', 'tool_wrong_args', 'false_claim', 'policy_explain', 'language_tone', 'latency', 'model_error', 'product_gap', 'not_a_bug']
const defaultExpected = { outcome: 'free', forbid_text: ['已执行'] }

export function BadcasePanel({ settings }: { settings: Settings }) {
  const [rows, setRows] = useState<any[]>([])
  const [selected, setSelected] = useState<any>(null)
  const [status, setStatus] = useState('new')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [rootCause, setRootCause] = useState('false_claim')
  const [severity, setSeverity] = useState('P2')
  const [expected, setExpected] = useState(JSON.stringify(defaultExpected))
  const [note, setNote] = useState('')

  useEffect(() => {
    setRootCause(selected?.label?.root_cause ?? 'false_claim')
    setSeverity(selected?.label?.severity ?? 'P2')
    setExpected(JSON.stringify(selected?.label?.expected ?? defaultExpected, null, 2))
    setNote(selected?.label?.note ?? '')
  }, [selected])

  async function load(filter = status) {
    setError('')
    try { setRows((await requestFor(settings.backend, settings, '/admin/badcases?' + new URLSearchParams(filter ? { status: filter } : {}))).items) }
    catch (cause) { setError(String(cause)) }
  }
  useEffect(() => { load() }, [])  // eslint-disable-line react-hooks/exhaustive-deps

  async function act(action: string) {
    if (!selected) return
    setBusy(true); setError('')
    try {
      const body = action === 'label' ? { root_cause: rootCause, severity, expected: JSON.parse(expected), note } : {}
      setSelected(await requestFor(settings.backend, settings, `/admin/badcases/${selected.id}/${action}`, jsonBody(body)))
      await load()
    } catch (cause) { setError(String(cause)) }
    finally { setBusy(false) }
  }

  return (
    <div className="grid gap-5 lg:grid-cols-[minmax(0,320px)_minmax(0,1fr)]">
      <Panel title="样本" icon={<FlagIcon />} actions={<Button type="button" size="sm" variant="ghost" onClick={() => load()}><RefreshCwIcon className="size-3.5" />刷新</Button>}>
        <SelectInput label="状态" value={status || 'all'} onChange={value => { const next = value === 'all' ? '' : value; setStatus(next); load(next) }}
          options={[{ id: 'all', label: '全部' }, ...states.map(value => ({ id: value, label: value }))]} />
        {error && <div className="mt-3"><ErrorAlert>{error}</ErrorAlert></div>}
        <ul className="mt-3 grid gap-1.5">
          {rows.map(row => (
            <li key={row.id}>
              <button type="button" onClick={() => setSelected(row)}
                className={`w-full rounded-md border px-3 py-2.5 text-left transition hover:bg-muted ${selected?.id === row.id ? 'border-accent bg-accent/40' : 'border-border'}`}>
                <code className="block truncate text-[13px]">{row.request_id}</code>
                <span className="mt-1 flex items-center gap-1.5 text-[13px] text-muted-foreground">{row.source}<StatusChip tone={stateTones[row.status] || 'default'}>{row.status}</StatusChip></span>
              </button>
            </li>
          ))}
        </ul>
        {!rows.length && <p className="mt-3 text-sm text-muted-foreground">暂无样本。</p>}
      </Panel>
      {!selected ? <Empty icon={<FlagIcon />} title="选择一个样本开始标注" description="标注根因与期望结果后，可提升为回归用例。" /> : (
        <Panel title={<code className="text-sm">{selected.request_id}</code>} actions={<StatusChip tone={stateTones[selected.status] || 'default'}>{selected.status}</StatusChip>}>
          <div className="flex flex-col gap-4">
            <p className="text-sm">信号：{selected.signals.join('、') || '成功抽样'}</p>
            {selected.snapshot.snapshot_partial && <InfoAlert status="warning">正文已过期，仅保留回合元数据，不能提升为回归用例。</InfoAlert>}
            <Mono>{JSON.stringify(selected.snapshot, null, 2)}</Mono>
            {selected.labeler && <p className="text-[13px] text-muted-foreground">当前标注：{selected.labeler} · {selected.label.root_cause}</p>}
            {!['promoted', 'dismissed'].includes(selected.status) && (
              <form className="flex flex-col gap-4" onSubmit={event => { event.preventDefault(); act('label') }}>
                <div className="grid gap-3 sm:grid-cols-2">
                  <SelectInput label="根因" value={rootCause} onChange={setRootCause} options={causes.map(cause => ({ id: cause, label: cause }))} />
                  <SelectInput label="严重级别" value={severity} onChange={setSeverity} options={['P0', 'P1', 'P2', 'P3'].map(level => ({ id: level, label: level }))} />
                </div>
                <TextAreaInput label="期望结果（JSON）" value={expected} onChange={setExpected} rows={6} isRequired className="[&_textarea]:font-mono [&_textarea]:text-[13px]"
                  description="字段：intent、tools、forbid_tools、outcome、forbid_text、evidence；授权用例另填 system 和 role。" />
                <TextAreaInput label="标注说明" value={note} onChange={setNote} maxLength={2000} rows={3} />
                <div className="flex flex-wrap gap-2">
                  <Button type="submit" disabled={busy}>保存标注</Button>
                  <Button type="button" variant="outline" disabled={busy} onClick={() => act('dismiss')}>排除样本</Button>
                  {selected.status === 'labeled' && <Button type="button" variant="secondary" disabled={busy} onClick={() => act('promote')}>提升为回归用例</Button>}
                </div>
              </form>
            )}
          </div>
        </Panel>
      )}
    </div>
  )
}
