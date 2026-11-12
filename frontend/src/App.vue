<template>
  <div class="app-shell" :class="`view-${activeView}`">
    <aside class="navigation-rail" aria-label="主导航">
      <a class="brand" href="#" @click.prevent="activeView = 'chat'" aria-label="IT-Copilot 首页">
        <span class="brand-mark"><AppIcon name="it" /></span>
        <span class="brand-name">IT-Copilot</span>
      </a>
      <button class="new-chat-button" @click="activeView = 'chat'; chat.clear()">
        <AppIcon name="plus" />
        <span>新对话</span>
      </button>
      <span class="nav-caption">工作空间</span>
      <nav class="view-nav">
        <button
          v-for="view in views"
          :key="view.id"
          :aria-label="view.label"
          :class="{ active: activeView === view.id }"
          :aria-current="activeView === view.id ? 'page' : undefined"
          @click="activeView = view.id"
        >
          <AppIcon :name="view.icon" />
          <span>{{ view.label }}</span>
          <AppIcon class="nav-chevron" name="chevron" />
        </button>
      </nav>
      <div class="rail-bottom">
        <a class="docs-link" aria-label="API 文档" :href="docsUrl" target="_blank" rel="noreferrer">
          <AppIcon name="document" />
          <span>API 文档</span>
          <AppIcon name="northeast" />
        </a>
        <button class="user-button" aria-label="当前用户与会话配置" @click="openConnection">
          <span class="avatar">{{ userInitial }}</span>
          <span>
            <strong>{{ auth.state.identity?.username || '未登录' }}</strong>
            <small>{{ identity ? `${identity.department} · ${formatRoles(identity.roles)}` : '点击登录' }}</small>
          </span>
          <AppIcon name="sliders" />
        </button>
      </div>
    </aside>
    <main class="workspace">
      <header class="topbar">
        <div class="page-breadcrumb">
          <span>工作空间</span>
          <AppIcon name="chevron" />
          <strong>{{ currentView.label }}</strong>
        </div>
        <a class="mobile-brand" href="#" @click.prevent="activeView = 'chat'" aria-label="IT-Copilot 首页">
          <span class="brand-mark"><AppIcon name="it" /></span>
          <span>IT-Copilot</span>
        </a>
        <div class="topbar-tools">
          <span class="connection-status" :title="health.detail || health.label">
            <i :class="health.ok ? 'online' : 'offline'"></i>
            {{ health.ok ? '已连接' : health.label === '未检查' ? '连接中' : '未连接' }}
          </span>
          <button class="icon-button settings-button" aria-label="连接配置" title="连接配置" @click="openConnection">
            <AppIcon name="sliders" />
          </button>
        </div>
      </header>
      <div v-if="toast" class="toast" role="status">
        <AppIcon name="chat" />
        {{ toast }}
      </div>
      <ChatView v-if="activeView === 'chat'" :key="viewKey" :session="chat" :settings="settings" :identity="identity"
        :backend-label="currentBackend.label" :monitor-data="monitorData" :task-history-client="taskHistoryClient"
        @open-task="openTask" @refresh-monitor="loadMonitor" @toast="showToast" />
      <TaskWorkspace v-else-if="activeView === 'tasks'" :key="viewKey" :settings="settings" :identity="identity" />
      <KnowledgeView v-else-if="activeView === 'knowledge'" :key="viewKey" :request-state="requestState" :identity="identity" @toast="showToast" @status="health.detail = $event" />
      <ApprovalsView v-else-if="activeView === 'approvals'" :key="viewKey" :settings="settings" :request-state="requestState" :identity="identity"
        @approval="(target, decision, note) => applyApprovalAction(target, settings, decision, note)" @toast="showToast" />
      <OperationsView v-else :key="viewKey" :settings="settings" :identity="identity" :monitor-data="monitorData" @refresh="loadMonitor" />
    </main>
    <ConnectionDialog ref="connectionDialog" :backend-label="currentBackend.label" :auth="auth" :health="health"
      @save="saveConnection" @check-health="checkHealth" @toast="showToast" />
  </div>
</template>

<script setup lang="ts">
// App shell: navigation, connection + identity, and the state shared across pages
// (chat session, monitor data, toasts). Each page owns everything else it shows.
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { backendMeta } from './api/http.ts'
import { createInitialSettings, saveSettings } from './api/settings.ts'
import { requestChatStream, requestToolTrace } from './api/chat.ts'
import { requestAuthConfig, requestDevToken, requestHealth, requestMe, requestMonitor } from './api/system.ts'
import { completeOidcLogin, oidcLogout, startOidcLogin, storeToken } from './lib/auth.ts'

import AppIcon from './components/AppIcon.vue'
import ConnectionDialog, { type ConnectionDraft, type HealthState } from './components/ConnectionDialog.vue'
import TaskWorkspace from './components/TaskWorkspace.vue'
import ApprovalsView from './views/ApprovalsView.vue'
import ChatView from './views/ChatView.vue'
import KnowledgeView from './views/KnowledgeView.vue'
import OperationsView from './views/OperationsView.vue'

import { createAuthSession } from './lib/authSession.ts'
import { createChatSession } from './lib/chatSession.ts'
import { createRequestState, loadCurrent } from './lib/requestState.ts'
import { createTaskClient } from './lib/taskClient.ts'
import { applyApprovalAction } from './lib/approvalDecision.ts'
import { formatRoles } from './lib/format.ts'
import type { MonitorData, Settings } from './lib/types.ts'

const settings = reactive<Settings>(createInitialSettings())
const requestState = createRequestState(settings)
const persist = () => saveSettings(settings)

const chat = createChatSession(settings, requestState,
  { stream: requestChatStream, trace: requestToolTrace, loadApproval: card => applyApprovalAction(card, settings) },
  { persist, onReply: () => loadMonitor() })
const auth = createAuthSession(settings, requestState,
  { config: requestAuthConfig, me: requestMe, devToken: requestDevToken, completeOidcLogin, startOidcLogin, oidcLogout, storeToken },
  { toast: showToast, onIdentityChange: () => chat.clear() })
const identity = computed(() => auth.state.identity)

const views: { id: string; label: string; icon: 'chat' | 'book' | 'shield' | 'chart' }[] = [
  { id: 'chat', label: '对话', icon: 'chat' },
  { id: 'tasks', label: '共享事项', icon: 'chat' },
  { id: 'knowledge', label: '知识库', icon: 'book' },
  { id: 'approvals', label: '审批', icon: 'shield' },
  { id: 'evaluation', label: '运行状态', icon: 'chart' }
]
const activeView = ref(new URL(window.location.href).searchParams.has('task') ? 'tasks' : 'chat')
const currentView = computed(() => views.find(view => view.id === activeView.value)!)
const currentBackend = computed(() => backendMeta(settings.backend, settings))
// Pages keyed by connection + identity remount (and drop their data) whenever either changes.
const viewKey = computed(() => `${currentBackend.value.baseUrl}:${settings.accessToken}`)
const docsUrl = computed(() => `${currentBackend.value.baseUrl}/docs`)
const userInitial = computed(() => (identity.value?.username || '?').slice(0, 1).toUpperCase())
const taskHistoryClient = computed(() => createTaskClient(settings))

const health = reactive<HealthState>({ ok: false, label: '未检查', detail: '' })
const emptyMonitor = (): MonitorData => ({ agent: {}, tools: {}, monitor: null, mcp: { servers: [] } })
const monitorData = ref<MonitorData>(emptyMonitor())
const connectionDialog = ref<InstanceType<typeof ConnectionDialog> | null>(null)
const toast = ref('')
let toastTimer: ReturnType<typeof setTimeout> | undefined

watch(() => settings.conversationId, persist)
onMounted(async () => {
  await auth.init()
  refreshConsole()
})
onBeforeUnmount(() => {
  requestState.invalidate()
  clearTimeout(toastTimer)
})

function openTask(id: string) {
  const url = new URL(window.location.href)
  url.searchParams.set('task', id)
  window.history.replaceState({}, '', url)
  activeView.value = 'tasks'
}

function openConnection() {
  connectionDialog.value?.open({ endpoint: currentBackend.value.baseUrl, conversationId: settings.conversationId })
}

function saveConnection({ endpoint, conversationId }: ConnectionDraft) {
  const endpointChanged = endpoint !== currentBackend.value.baseUrl
  if (endpointChanged || conversationId !== settings.conversationId) {
    chat.reset()
    monitorData.value = emptyMonitor()
    Object.assign(health, { ok: false, label: '未检查', detail: '' })
  }
  settings.endpoints[settings.backend] = endpoint
  // 换服务时沿用旧会话 ID 没有意义：除非用户改了它，否则重新开始。
  settings.conversationId = endpointChanged && conversationId === settings.conversationId ? '' : conversationId
  persist()
  refreshConsole()
  // 另一个服务的登录方式和身份可能不同：重新读取登录配置并校验令牌。
  if (endpointChanged) auth.init()
  showToast('连接配置已保存')
}

async function refreshConsole() {
  // /monitor 需要登录；未登录时只检查公开接口，避免无意义的 401。知识库统计与 Skills 由知识库页自行加载。
  await Promise.allSettled([checkHealth(), settings.accessToken ? loadMonitor() : null])
}

function checkHealth() {
  // 健康检查只更新状态，保留用户选择的后端。
  return loadCurrent<{ status?: string }>(requestState, 'health', requestHealth,
    data => Object.assign(health, { ok: data.status === 'ok', label: data.status || 'ok', detail: JSON.stringify(data, null, 2) }),
    error => Object.assign(health, { ok: false, label: '不可用', detail: error.message }))
}

function loadMonitor() {
  return loadCurrent<MonitorData>(requestState, 'monitor', requestMonitor,
    data => { monitorData.value = { ...data, loaded: true } },
    () => { monitorData.value = emptyMonitor() })
}

function showToast(message: string) {
  toast.value = message
  clearTimeout(toastTimer)
  toastTimer = setTimeout(() => { toast.value = '' }, 2600)
}
</script>
