// App shell: navigation, connection + identity, and the state shared across pages
// (chat session and toasts). Each page owns everything else it shows.
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { Button } from '@/components/ui/button'
import { Sidebar, SidebarInset, SidebarProvider, SidebarMenu, SidebarMenuItem, SidebarMenuButton, useSidebar } from '@/components/ui/sidebar'
import { Header } from '@/components/layout/header'
import { Outlet, useNavigate, useRouterState } from '@tanstack/react-router'
import { QueryClientProvider, useQuery } from '@tanstack/react-query'
import { createServerQueryClient } from './lib/queryClient'
import { viewPaths, viewForPath, type ViewId } from './lib/navigation'
import { Toaster, toast } from 'sonner'
import { TooltipProvider } from '@/components/ui/tooltip'
import { BookOpenIcon, ExternalLinkIcon, FileTextIcon, MessageSquareIcon, MoonIcon, PanelRightIcon, PencilIcon, SearchIcon, ShieldCheckIcon, SlidersHorizontalIcon, SquarePenIcon, SunIcon, Trash2Icon, XIcon } from 'lucide-react'
import { useStore } from './hooks/useStore.ts'
import { backendMeta, requestFor } from './api/http.ts'
import { createInitialSettings, saveSettings } from './api/settings.ts'
import { requestChatStream, requestToolTrace } from './api/chat.ts'
import { deleteConversation, getConversation, listConversations, renameConversation, type ConversationSummary } from './api/conversations.ts'
import { requestAuthConfig, requestDevToken, requestHealth, requestMe } from './api/system.ts'
import { completeOidcLogin, oidcLogout, startOidcLogin, storeToken } from './lib/auth.ts'
import { createAuthSession } from './lib/authSession.ts'
import { createChatSession } from './lib/chatSession.ts'
import { createRequestState, loadCurrent } from './lib/requestState.ts'
import { applyApprovalAction } from './lib/approvalDecision.ts'
import { formatRoles } from './lib/format.ts'
import { reactive } from './lib/store.ts'
import type { Identity, Settings } from './lib/types.ts'
import { Hint } from './components/ui.tsx'
import { ConnectionDialog, type ConnectionDraft, type HealthState } from './components/ConnectionDialog.tsx'
import { ApprovalsView } from './views/ApprovalsView.tsx'
import { ChatView } from './views/ChatView.tsx'
import { KnowledgeView } from './views/KnowledgeView.tsx'

const views: { id: ViewId; label: string; icon: ReactNode; hint: string }[] = [
  { id: 'chat', label: '对话', icon: <MessageSquareIcon />, hint: '私人 IT 服务对话' },
  { id: 'knowledge', label: '知识库', icon: <BookOpenIcon />, hint: '检索与管理 IT 知识' },
  { id: 'approvals', label: '审批', icon: <ShieldCheckIcon />, hint: '确认、审批与回收' },
]
const THEME_KEY = 'servicedesk-agent.theme'

function showToast(message: string) { toast(message) }

function relativeTime(seconds: number): string {
  const diff = Date.now() / 1000 - seconds
  if (diff < 60) return '刚刚'
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  if (diff < 172800) return '昨天'
  const date = new Date(seconds * 1000)
  return `${date.getMonth() + 1}月${date.getDate()}日`
}

// Long-lived runtime objects: created once per page load, like the module-level setup in the Vue app.
function createRuntime() {
  const settings = reactive<Settings>(createInitialSettings())
  const requestState = createRequestState(settings)
  const persist = () => saveSettings(settings)
  const history = { load: () => Promise.resolve() }
  const chat = createChatSession(settings, requestState,
    { stream: requestChatStream, trace: requestToolTrace, loadApproval: card => applyApprovalAction(card, settings) },
    { persist, onReply: () => history.load() })
  const auth = createAuthSession(settings, requestState,
    { config: requestAuthConfig, me: requestMe, devToken: requestDevToken, completeOidcLogin, startOidcLogin, oidcLogout, storeToken },
    { toast: showToast, onIdentityChange: () => chat.clear() })
  return { settings, requestState, persist, chat, auth, history }
}

export function App() {
  return <SidebarProvider className="h-svh min-h-0" style={{ '--sidebar-width': '18rem' } as React.CSSProperties}><AppRuntime /></SidebarProvider>
}

function AppRuntime() {
  const runtime = useMemo(createRuntime, [])
  const settings = useStore(runtime.settings)
  const authState = useStore(runtime.auth.state)
  useEffect(() => {
    void runtime.auth.init()
    return () => runtime.requestState.invalidate()
  }, [runtime])
  const scope = `${backendMeta(settings.backend, settings).baseUrl}:${settings.accessToken}`
  return <AppWorkspace key={scope} runtime={runtime} identity={authState.identity} />
}

function AppWorkspace({ runtime, identity }: { runtime: ReturnType<typeof createRuntime>; identity: Identity | null }) {
  const routeNavigate = useNavigate()
  const pathname = useRouterState({ select: state => state.location.pathname })

  const activeView = viewForPath(pathname)
  const { setOpenMobile } = useSidebar()
  const { requestState, persist, chat, auth } = runtime
  const settings = useStore(runtime.settings)

  const [health, setHealth] = useState<HealthState>({ ok: false, label: '未检查', detail: '' })
  const [connectionOpen, setConnectionOpen] = useState(false)
  const [historyQuery, setHistoryQuery] = useState('')
  const [searchText, setSearchText] = useState('')
  const [chatsLimit, setChatsLimit] = useState(30)
  const [panelOpen, setPanelOpen] = useState(true)
  const [panelDrawer, setPanelDrawer] = useState(false)
  const [dark, setDark] = useState(() => localStorage.getItem(THEME_KEY) !== 'light')

  const currentBackend = backendMeta(settings.backend, settings)
  // Pages keyed by connection + identity remount (and drop their data) whenever either changes.
  const viewKey = `${currentBackend.baseUrl}:${settings.accessToken}`
  const currentView = views.find(view => view.id === activeView)!
  const userInitial = (identity?.username || '?').slice(0, 1).toUpperCase()
  const chatState = useStore(chat.state)

  // Each endpoint/token gets a fresh cache; late responses can only populate their old client.
  const queryClient = useMemo(createServerQueryClient, [])
  useEffect(() => () => { queryClient.clear() }, [queryClient])
  const snapshot = useMemo(() => ({ ...runtime.settings, endpoints: { ...runtime.settings.endpoints } }), [viewKey])
  const authenticated = !!identity && !!snapshot.accessToken
  useEffect(() => {
    const timer = setTimeout(() => setHistoryQuery(searchText.trim()), 300)
    return () => clearTimeout(timer)
  }, [searchText])
  const catalog = useQuery({ queryKey: ['models'], enabled: authenticated && activeView === 'chat',
    queryFn: ({ signal }) => requestFor<{ models: { id: string; name: string; configured: boolean }[];
      defaults: { agent_read: { model: string }; agent_write: { model: string } } }>(snapshot.backend, snapshot, '/models', { signal }) }, queryClient)
  useEffect(() => { runtime.settings.model = undefined }, [viewKey, runtime])
  const chatsResult = useQuery({ queryKey: ['conversations', historyQuery, chatsLimit], enabled: authenticated,
    queryFn: ({ signal }) => listConversations(snapshot.backend, snapshot, { q: historyQuery, limit: chatsLimit }, signal) }, queryClient)
  const chats = chatsResult.isError ? [] : chatsResult.data?.items || []
  const chatsMore = !chatsResult.isError && !!chatsResult.data?.has_more
  const loadChats = useCallback(async () => { await queryClient.invalidateQueries({ queryKey: ['conversations'] }) }, [queryClient])
  runtime.history.load = loadChats

  const conversationRead = useRef(0)
  useEffect(() => () => { conversationRead.current++ }, [])
  async function openConversation(id: string) {
    const generation = ++conversationRead.current
    navigate('chat')
    if (id === runtime.settings.conversationId && chatState.messages.length) return
    try {
      const stored = await getConversation(snapshot.backend, snapshot, id)
      if (generation === conversationRead.current) chat.open(id, stored.messages)
    } catch (error) { if (generation === conversationRead.current) showToast(`打开对话失败：${error instanceof Error ? error.message : String(error)}`) }
  }

  function newConversation() {
    conversationRead.current++
    chat.clear()
    navigate('chat')
  }

  async function renameChat(item: ConversationSummary) {
    const title = (window.prompt('重命名对话', item.title) || '').trim()
    if (!title || title === item.title) return
    try { await renameConversation(runtime.settings.backend, runtime.settings, item.conv_id, title); loadChats() }
    catch (error) { showToast(`重命名失败：${error instanceof Error ? error.message : String(error)}`) }
  }

  async function deleteChat(item: ConversationSummary) {
    if (!window.confirm(`删除对话「${item.title}」？删除后无法恢复，已提交的审批和工单不受影响。`)) return
    try {
      await deleteConversation(runtime.settings.backend, runtime.settings, item.conv_id)
      if (item.conv_id === runtime.settings.conversationId) chat.clear()
      loadChats()
    } catch (error) { showToast(`删除失败：${error instanceof Error ? error.message : String(error)}`) }
  }

  useEffect(() => {
    document.documentElement.classList.toggle('dark', dark)
    document.documentElement.dataset.theme = dark ? 'dark' : 'light'
    localStorage.setItem(THEME_KEY, dark ? 'dark' : 'light')
  }, [dark])

  const checkHealth = useCallback(() => loadCurrent<{ status?: string }>(requestState, 'health', requestHealth,
    data => setHealth({ ok: data.status === 'ok', label: data.status || 'ok', detail: JSON.stringify(data, null, 2) }),
    error => setHealth({ ok: false, label: '不可用', detail: error.message })), [requestState])

  const refreshConsole = useCallback(async () => {
    await checkHealth()
  }, [checkHealth])

  useEffect(() => { void checkHealth() }, [checkHealth])
  useEffect(() => { persist() }, [settings.conversationId, persist])

  function navigate(view: ViewId) {
    void routeNavigate({ to: viewPaths[view], search: {} }); setOpenMobile(false)
  }


  function togglePanel() {
    if (window.matchMedia('(min-width: 1280px)').matches) setPanelOpen(open => !open)
    else setPanelDrawer(true)
  }

  function saveConnection({ endpoint, conversationId }: ConnectionDraft) {
    const endpointChanged = endpoint !== currentBackend.baseUrl
    if (endpointChanged || conversationId !== settings.conversationId) {
      conversationRead.current++
      chat.reset()
      queryClient.clear()
      setHealth({ ok: false, label: '未检查', detail: '' })
    }
    settings.endpoints[settings.backend] = endpoint
    // 换服务时沿用旧会话 ID 没有意义：除非用户改了它，否则重新开始。
    settings.conversationId = endpointChanged && conversationId === settings.conversationId ? '' : conversationId
    persist()
    if (!endpointChanged) void refreshConsole()
    // 另一个服务的登录方式和身份可能不同：重新读取登录配置并校验令牌。
    if (endpointChanged) auth.init()
    showToast('连接配置已保存')
  }

  const statusText = health.ok ? '已连接' : health.label === '未检查' ? '连接中' : '未连接'

  const sidebar = (
    <div className="flex h-full flex-col">
      <div className="flex h-16 shrink-0 items-center justify-between px-4">
        <button type="button" onClick={() => navigate('chat')} className="flex items-center text-left" aria-label="ServiceDesk Agent 首页">
          <span className="brand-mark mr-2" aria-hidden="true" /><span className="text-base font-semibold tracking-tight">ServiceDesk Agent</span>
        </button>
        <Hint label={<>新对话</>}><Button type="button" size="icon" variant="ghost" aria-label="新对话" onClick={newConversation}><SquarePenIcon className="size-4" /></Button></Hint>
      </div>
      <nav className="px-2.5 pt-1" aria-label="主导航"><SidebarMenu>
        {views.map(view => {
          const active = activeView === view.id
          return (
            <SidebarMenuItem key={view.id}><SidebarMenuButton type="button" isActive={active} aria-current={active ? 'page' : undefined} onClick={() => navigate(view.id)}
              className={`flex h-9 items-center gap-3 rounded-lg px-2.5 text-left text-sm transition-colors focus-visible:ring-1 focus-visible:ring-ring focus-visible:outline-none [&>svg]:size-4 ${
                active ? 'bg-accent font-medium text-foreground' : 'text-muted-foreground hover:bg-accent/60 hover:text-foreground'}`}>
              {view.icon}
              <span className="flex-1">{view.label}</span>
              {view.id === 'chat' && chatState.messages.length > 0 && <span className="text-xs tabular-nums text-muted-foreground">{chatState.messages.length}</span>}
            </SidebarMenuButton></SidebarMenuItem>
          )
        })}
      </SidebarMenu></nav>
      <div className="mt-5 border-t pt-5 flex min-h-0 flex-1 flex-col">
        <div className="flex items-center justify-between gap-2 px-3 pb-2">
          <span className="text-xs font-medium text-muted-foreground">对话记录</span>
        </div>
        <div className="flex flex-col gap-2 px-3 pb-2">
          <label className="flex h-8 items-center gap-2 rounded-lg border border-border bg-background px-2.5 text-muted-foreground focus-within:border-ring">
            <SearchIcon className="size-3.5 shrink-0" />
            <input value={searchText} onChange={event => setSearchText(event.target.value)} maxLength={200}
              placeholder="搜索对话" aria-label="搜索对话"
              className="min-w-0 flex-1 bg-transparent text-[13px] text-foreground outline-none placeholder:text-muted-foreground" />
            {searchText && <button type="button" aria-label="清除搜索" onClick={() => setSearchText('')} className="text-muted-foreground hover:text-foreground"><XIcon className="size-3" /></button>}
          </label>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-2.5 pb-2">
              {chats.map(item => {
                const active = activeView === 'chat' && settings.conversationId === item.conv_id
                return (
                  <div key={item.conv_id} className={`group relative mb-0.5 rounded-lg transition-colors ${active ? 'bg-accent' : 'hover:bg-accent/60'}`}>
                    <button type="button" onClick={() => openConversation(item.conv_id)} className="flex w-full flex-col gap-0.5 px-2.5 py-2 text-left">
                      <span className="line-clamp-1 pr-12 text-sm text-foreground">{item.title}</span>
                      <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
                        <span>{relativeTime(item.updated_at)}</span>
                        <span className="text-muted-foreground/50">·</span>
                        <span>{Math.ceil(item.message_count / 2)} 轮</span>
                      </span>
                    </button>
                    <div className="absolute top-1.5 right-1.5 hidden items-center group-focus-within:flex group-hover:flex">
                      <Button type="button" size="icon" variant="ghost" aria-label="重命名对话" className="size-6" onClick={() => renameChat(item)}><PencilIcon className="size-3" /></Button>
                      <Button type="button" size="icon" variant="ghost" aria-label="删除对话" className="size-6 hover:text-destructive-foreground" onClick={() => deleteChat(item)}><Trash2Icon className="size-3" /></Button>
                    </div>
                  </div>
                )
              })}
              {chatsResult.isError && <p role="alert" className="px-2.5 py-2 text-xs text-destructive">对话记录加载失败：{chatsResult.error.message}</p>}
              {!chatsResult.isError && !chats.length && <p className="px-2.5 py-3 text-xs text-muted-foreground">{!identity ? '登录后显示对话记录' : searchText ? '没有匹配的对话' : '还没有对话记录，发送第一条消息后会出现在这里。'}</p>}
              {chatsMore && <Button type="button" size="sm" variant="ghost" className="w-full mt-1 text-muted-foreground" onClick={() => setChatsLimit(limit => limit + 30)}>加载更多</Button>}
        </div>
      </div>
      <div className="flex flex-col gap-0.5 border-t border-border p-2.5">
        <a href={`${currentBackend.baseUrl}/docs`} target="_blank" rel="noreferrer" className="flex h-8 items-center gap-3 rounded-lg px-2.5 text-sm text-muted-foreground no-underline hover:bg-accent/60 hover:text-foreground">
          <FileTextIcon className="size-4" />API 文档<ExternalLinkIcon className="ml-auto size-3" />
        </a>
        <button type="button" onClick={() => setConnectionOpen(true)} aria-label="当前用户与会话配置"
          className="flex items-center gap-2.5 rounded-xl border bg-card surface-edge px-3 py-3 text-left transition-colors hover:bg-accent/60">
          <span className="relative grid size-8 shrink-0 place-items-center rounded-full bg-accent text-xs font-medium">{userInitial}{identity && <i className="absolute bottom-0 right-0 size-2 rounded-full bg-success ring-2 ring-card" />}</span>
          <span className="min-w-0 flex-1">
            <span className="block truncate text-sm leading-tight">{identity?.username || '未登录'}</span>
            <span className="block truncate text-xs leading-tight text-muted-foreground">{identity ? <>{identity.department} <span className="inline-block rounded-full border bg-secondary px-1.5">{formatRoles(identity.roles)}</span></> : '点击登录'}</span>
          </span>
          <SlidersHorizontalIcon className="size-4 text-muted-foreground" />
        </button>
      </div>
    </div>
  )

  let page: ReactNode
  if (activeView === 'chat') {
    page = <ChatView key={viewKey} session={chat} settings={settings} identity={identity} backendLabel={currentBackend.label}
      onToast={showToast}
      panelOpen={panelOpen} panelDrawer={panelDrawer} onPanelDrawerChange={setPanelDrawer} />
  } else if (activeView === 'knowledge') {
    page = <KnowledgeView key={viewKey} requestState={requestState} identity={identity} onToast={showToast} onStatus={detail => setHealth(current => ({ ...current, detail }))} />
  } else {
    page = <ApprovalsView key={viewKey} settings={settings} identity={identity}
      onApproval={(target, decision, note) => applyApprovalAction(target, settings, decision, note)} />
  }
  if (pathname !== '/' && !Object.values(viewPaths).some(path => path === pathname.replace(/\/$/, ''))) page = <Outlet />
  const fullBleed = activeView === 'chat'

  return (
    <QueryClientProvider client={queryClient}>
    <TooltipProvider delay={300}>
      <Toaster position="bottom-right" theme={dark ? 'dark' : 'light'} richColors />
      <Sidebar>{sidebar}</Sidebar>
      <SidebarInset className="h-svh min-w-0 overflow-hidden">
        <Header className="shrink-0 border-b [&>div>div:last-child]:ml-auto">
          <div className="flex min-w-0 items-center gap-2 text-sm">
            <span className="hidden text-muted-foreground sm:inline">ServiceDesk Agent</span>
            <span className="hidden text-muted-foreground/60 sm:inline">/</span>
            <span className="truncate font-medium">{activeView === 'chat' && chatState.messages.length ? (chatState.messages[0]?.content.slice(0, 40) || currentView.label) : currentView.label}</span>
          </div>
          <div className="flex items-center gap-1">
            {activeView === 'chat' && chatState.lastResponse?.raw?.model &&
              <span className="text-xs text-muted-foreground">上次回复 · {chatState.lastResponse.raw.model}</span>}
            {activeView === 'chat' && catalog.data?.defaults && <select aria-label="对话模型"
              className="rounded-md border bg-background px-2 py-1 text-xs" value={settings.model || ''}
              onChange={event => { runtime.settings.model = event.target.value || undefined }}>
              <option value="">默认 · {catalog.data.defaults.agent_read.model}{catalog.data.defaults.agent_read.model !== catalog.data.defaults.agent_write.model && ` / ${catalog.data.defaults.agent_write.model}`}</option>
              {catalog.data.models.map(model => <option key={model.id} value={model.id} disabled={!model.configured}>{model.name}</option>)}
            </select>}
            <Hint label={<>{health.ok ? `${currentBackend.label} 服务 · ${currentBackend.baseUrl}` : health.detail || health.label}</>}><span className="inline-flex h-6 shrink-0 cursor-default items-center gap-1.5 rounded-full border bg-card px-2 text-xs text-muted-foreground">
                <span className={`size-1.5 rounded-full ${health.ok ? 'bg-success ring-2 ring-success/15' : health.label === '未检查' ? 'bg-muted' : 'bg-destructive'}`} />
                {statusText}
              </span></Hint>
            {activeView === 'chat' && (
              <Hint label={<>运行详情</>}><Button type="button" size="icon" variant={panelOpen ? 'secondary' : 'ghost'} aria-label="运行详情" onClick={togglePanel}><PanelRightIcon className="size-4" /></Button></Hint>
            )}
            <Hint label={<>{dark ? '浅色模式' : '深色模式'}</>}><Button type="button" size="icon" variant="ghost" aria-label={dark ? '切换到浅色模式' : '切换到深色模式'} onClick={() => setDark(!dark)}>
                {dark ? <SunIcon className="size-4" /> : <MoonIcon className="size-4" />}
              </Button></Hint>
            <Hint label={<>连接配置</>}><Button type="button" size="icon" variant="ghost" aria-label="连接配置" onClick={() => setConnectionOpen(true)}><SlidersHorizontalIcon className="size-4" /></Button></Hint>
          </div>
        </Header>
        <div className={`min-h-0 flex-1 ${fullBleed ? 'overflow-hidden' : 'overflow-y-auto'}`}>{page}</div>
      </SidebarInset>

      <ConnectionDialog isOpen={connectionOpen} onOpenChange={setConnectionOpen} initial={{ endpoint: currentBackend.baseUrl, conversationId: settings.conversationId }}
        backendLabel={currentBackend.label} auth={auth} health={health} onSave={saveConnection} onCheckHealth={checkHealth} onToast={showToast} />
    </TooltipProvider>
    </QueryClientProvider>
  )
}
