// Keyed by connection + token in App: switching either remounts the view with fresh state.
import { useEffect, useRef, useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { BookOpenIcon, FileTextIcon, FileUpIcon, PlusIcon, RefreshCwIcon, SearchIcon, SparklesIcon, Trash2Icon } from 'lucide-react'
import { addKnowledge, deleteKnowledgeDocument, listKnowledgeDocuments, requestKnowledgeStats, requestSearch, uploadKnowledge } from '../api/knowledge.ts'
import { reloadSkills, requestSkills } from '../api/system.ts'
import { Empty, PageHeader, Panel, TextAreaInput, TextInput, errorText } from '../components/ui.tsx'
import { loadCurrent, type RequestState } from '../lib/requestState.ts'
import type { Identity } from '../lib/types.ts'

interface SearchResult { id?: string; title?: string; content: string; score?: number }
interface KnowledgeDoc { document_id: string; title: string; department: string; chunks: number; source: string; demo_seed?: boolean }
interface Skill { name: string; description?: string; content_chars?: number }
interface SkillSet { count: number; skills: Skill[]; errors: unknown[] }

const emptySkills = (): SkillSet => ({ count: 0, skills: [], errors: [] })

export function KnowledgeView({ requestState, identity, onToast, onStatus }: {
  requestState: RequestState; identity: Identity | null; onToast: (message: string) => void; onStatus: (text: string) => void
}) {
  const [count, setCount] = useState<number | string>('-')
  const [query, setQuery] = useState('VPN 连不上怎么办')
  const [results, setResults] = useState<SearchResult[]>([])
  const [searched, setSearched] = useState(false)
  const [title, setTitle] = useState('')
  const [content, setContent] = useState('')
  const [docs, setDocs] = useState<KnowledgeDoc[]>([])
  const [skills, setSkills] = useState<SkillSet>(emptySkills())
  const [pending, setPending] = useState('')
  const pendingRef = useRef('')
  const fileRef = useRef<HTMLInputElement | null>(null)
  const busy = pending !== ''
  const isAdmin = (identity?.roles || []).includes('it_admin')

  useEffect(() => { loadStats(); loadSkills() }, [])  // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { loadDocs() }, [isAdmin])  // eslint-disable-line react-hooks/exhaustive-deps

  // One knowledge action at a time; the slot is separate from chat so the two never invalidate each other.
  async function perform(name: string, action: () => Promise<void>) {
    if (pendingRef.current) return
    pendingRef.current = name; setPending(name)
    try { await action() } finally { pendingRef.current = ''; setPending('') }
  }

  function loadStats() {
    return loadCurrent<{ total_chunks?: number; totalChunks?: number }>(requestState, 'stats', requestKnowledgeStats,
      data => setCount(data.total_chunks ?? data.totalChunks ?? '-'), () => setCount('-'))
  }
  function loadSkills() {
    return loadCurrent<SkillSet>(requestState, 'skills', requestSkills, data => setSkills(data), () => setSkills(emptySkills()))
  }
  function loadDocs() {
    if (!isAdmin) { setDocs([]); return }
    return loadCurrent<{ items: KnowledgeDoc[] }>(requestState, 'knowledge-docs', listKnowledgeDocuments,
      body => setDocs(body.items), error => onToast(`文档列表加载失败：${error.message}`))
  }

  async function search() {
    if (!query.trim()) return
    const request = requestState.begin('knowledge-action')
    try {
      const data = await requestSearch(request.backend, request.settings, query, 5)
      if (!request.isCurrent()) return
      setResults(data.results || []); setSearched(true)
      onToast(`检索完成，返回 ${(data.results || []).length} 条结果`)
    } catch (error) {
      if (!request.isCurrent()) return
      onStatus(errorText(error)); onToast('检索失败，请检查连接')
    }
  }

  async function submit() {
    if (!title.trim() || !content.trim()) return
    const request = requestState.begin('knowledge-action')
    try {
      const data = await addKnowledge(request.backend, request.settings, [{ title: title.trim(), content: content.trim() }])
      if (!request.isCurrent()) return
      onStatus(JSON.stringify(data, null, 2))
      await Promise.all([loadStats(), loadDocs()])
      if (request.isCurrent()) { onToast('文档已添加'); setTitle(''); setContent('') }
    } catch (error) {
      if (!request.isCurrent()) return
      onStatus(errorText(error)); onToast('文档导入失败')
    }
  }

  function onFile(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file) return
    if (file.size > 10 * 1024 * 1024) { onToast('文件不能超过 10 MB'); return }
    perform('upload', async () => {
      const request = requestState.begin('knowledge-action')
      try {
        const data = await uploadKnowledge(request.backend, request.settings, file)
        if (!request.isCurrent()) return
        onStatus(JSON.stringify(data, null, 2))
        await Promise.all([loadStats(), loadDocs()])
        if (request.isCurrent()) onToast(`${file.name} 导入成功`)
      } catch (error) {
        if (!request.isCurrent()) return
        onStatus(errorText(error)); onToast('文件导入失败')
      }
    })
  }

  async function remove(doc: KnowledgeDoc) {
    const reseed = doc.demo_seed ? '\n这是演示种子文档，服务重启后会按 knowledge/ 目录重新导入。' : ''
    if (!window.confirm(`确认删除《${doc.title}》？Agent 将立即检索不到它。${reseed}`)) return
    const request = requestState.begin('knowledge-action')
    try {
      await deleteKnowledgeDocument(request.backend, request.settings, doc.document_id)
      if (!request.isCurrent()) return
      setDocs(items => items.filter(item => item.document_id !== doc.document_id))
      await loadStats()
      if (request.isCurrent()) onToast('文档已删除')
    } catch (error) {
      if (request.isCurrent()) onToast(`删除失败：${errorText(error)}`)
    }
  }

  async function reload() {
    const request = requestState.begin('knowledge-action')
    try {
      const data = await reloadSkills(request.backend, request.settings)
      if (!request.isCurrent()) return
      setSkills(data); onToast('Skills 已重新加载')
    } catch (error) {
      if (!request.isCurrent()) return
      onStatus(errorText(error)); onToast('Skills 加载失败')
    }
  }

  return (
    <section className="mx-auto w-full max-w-6xl px-4 py-8 sm:px-8">
      <PageHeader kicker="Knowledge" title="知识库" description="管理 IT 知识，为每次回答提供可追溯依据。"
        actions={
          <div className="flex items-center gap-3 rounded-lg border border-border bg-card px-4 py-2.5">
            <BookOpenIcon className="size-5 text-primary" />
            <div><p className="text-xl leading-none font-semibold tabular-nums">{count}</p><p className="mt-1 text-xs text-muted-foreground">知识片段</p></div>
          </div>
        } />

      <div className="grid gap-5 lg:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)]">
        <Panel kicker="Retrieval" title="检索知识" icon={<SearchIcon />}>
          <form className="flex items-end gap-2" onSubmit={event => { event.preventDefault(); perform('search', search) }}>
            <TextInput ariaLabel="检索问题" value={query} onChange={setQuery} placeholder="例如：VPN 连不上怎么办" />
            <Button type="submit" className="shrink-0" aria-busy={pending === 'search'} disabled={busy || !query.trim()}>
              <SearchIcon className="size-4" />{pending === 'search' ? '检索中' : '搜索'}
            </Button>
          </form>
          <div className="mt-4">
            {results.length ? (
              <ol className="grid gap-2.5">
                {results.map((item, index) => (
                  <li key={item.id || index} className="fade-up flex gap-3 rounded-md border border-border p-3.5">
                    <span className="grid size-7 shrink-0 place-items-center rounded-lg bg-muted text-[13px] font-semibold text-muted-foreground tabular-nums">{String(index + 1).padStart(2, '0')}</span>
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <strong className="text-sm">{item.title || '未命名文档'}</strong>
                        <Badge variant="outline" className="font-mono">相关度 {typeof item.score === 'number' ? item.score.toFixed(3) : '—'}</Badge>
                      </div>
                      <p className="mt-1.5 line-clamp-4 text-sm leading-relaxed text-muted-foreground">{item.content}</p>
                    </div>
                  </li>
                ))}
              </ol>
            ) : (
              <Empty icon={<BookOpenIcon />} title={searched ? '暂未找到相关知识' : '搜索你的知识库'}
                description={searched ? '尝试换一个问题，或添加更多知识文档。' : '输入员工 IT 问题，查看最相关的知识片段。'} />
            )}
          </div>
        </Panel>

        <Panel kicker="Ingestion" title="添加知识" icon={<FileTextIcon />}>
          <form className="flex flex-col gap-4" onSubmit={event => { event.preventDefault(); perform('import', submit) }}>
            <TextInput label="文档标题" value={title} onChange={setTitle} isRequired placeholder="例如：VPN 排障补充 SOP" />
            <TextAreaInput label="知识内容" value={content} onChange={setContent} isRequired rows={7} placeholder="输入 IT 规范、系统说明或排障流程" />
            <div className="flex flex-wrap gap-2">
              <Button type="submit" aria-busy={pending === 'import'} disabled={busy || !title.trim() || !content.trim()}><PlusIcon className="size-4" />添加文档</Button>
              <Button type="button" variant="secondary" aria-busy={pending === 'upload'} disabled={busy} onClick={() => fileRef.current?.click()}><FileUpIcon className="size-4" />上传文件</Button>
              <input ref={fileRef} hidden type="file" accept=".txt,.md,.json" onChange={onFile} />
            </div>
            <p className="text-[13px] text-muted-foreground">支持 TXT、Markdown、JSON，最大 10 MB。</p>
          </form>
        </Panel>
      </div>

      {isAdmin && (
        <Panel className="mt-5" kicker="Documents" title="文档管理" icon={<FileTextIcon />}
          actions={<Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => loadDocs()}><RefreshCwIcon className="size-3.5" />刷新</Button>}>
          {!docs.length ? <p className="text-sm text-muted-foreground">知识库中还没有文档。</p> : (
            <ul className="divide-y divide-border">
              {docs.map(doc => (
                <li key={doc.document_id} className="flex items-center justify-between gap-3 py-3">
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium">{doc.title}</p>
                    <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[13px] text-muted-foreground">
                      <Badge variant={doc.department === '*' ? 'secondary' : 'outline'}>{doc.department === '*' ? '全员可见' : doc.department}</Badge>
                      <span>{doc.chunks} 个片段 · 来源 {doc.source}</span>
                      {doc.demo_seed && <Badge variant="outline" className="text-warning-foreground border-warning/30">演示种子</Badge>}
                    </div>
                  </div>
                  <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => perform('delete-doc', () => remove(doc))}><Trash2Icon className="size-3.5" />删除</Button>
                </li>
              ))}
            </ul>
          )}
        </Panel>
      )}

      <Panel className="mt-5" kicker="Loaded Skills" title="已加载能力" icon={<SparklesIcon />}
        actions={<Button type="button" size="sm" variant="ghost" aria-busy={pending === 'skills'} disabled={busy} onClick={() => perform('skills', reload)}><RefreshCwIcon className="size-3.5" />重新加载</Button>}>
        {skills.skills?.length ? (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {skills.skills.map(skill => (
              <div key={skill.name} className="rounded-md border border-border p-3.5">
                <div className="flex items-center gap-2">
                  <span className="size-2 rounded-full bg-success" />
                  <strong className="truncate text-sm">{skill.name}</strong>
                </div>
                <p className="mt-1.5 line-clamp-2 text-[13px] text-muted-foreground">{skill.description || '业务规范能力'}</p>
                <p className="mt-2 text-xs text-muted-foreground tabular-nums">{skill.content_chars || 0} chars</p>
              </div>
            ))}
          </div>
        ) : <p className="text-sm text-muted-foreground">暂无已加载能力，连接服务后刷新查看。</p>}
      </Panel>
    </section>
  )
}
