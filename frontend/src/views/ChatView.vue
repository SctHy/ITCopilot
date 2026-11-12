<template>
  <section class="page-chat">
    <div class="chat-toolbar">
      <span class="conversation-label">
        {{ state.messages.length ? '当前对话' : '新对话' }}
        <span v-if="state.messages.length" class="conversation-count">{{ state.messages.length }} 条消息</span>
      </span>
      <div class="chat-toolbar-actions">
        <button class="icon-button" @click="session.clear()" aria-label="新对话" title="新对话">
          <AppIcon name="compose" />
        </button>
        <button class="trace-toggle" @click="inspectorDialog?.showModal()">
          <AppIcon name="panel" />
          <span>运行详情</span>
        </button>
      </div>
    </div>
    <div class="conversation-layout" :class="{ 'has-conversation': state.messages.length }">
      <section class="conversation" :class="{ 'is-empty': !state.messages.length }" aria-label="IT 服务台对话">
        <div class="messages" ref="messageList" role="log" aria-label="IT 服务台对话" aria-live="polite" :aria-busy="state.busy">
          <div v-if="!state.messages.length" class="welcome">
            <div class="welcome-mark" aria-hidden="true"><AppIcon name="it" /></div>
            <h1>今天，需要解决什么？</h1>
            <p>从员工问题开始，让合适的 Agent 接手。</p>
          </div>
          <template v-else>
            <ChatMessage
              v-for="item in state.messages"
              :key="item.id"
              :item="item"
              :identity="identity"
              :backend-label="backendLabel"
              @approval="(target, decision, note) => applyApprovalAction(target, settings, decision, note)"
              @copy="copyMessage"
              @open-task="emit('open-task', $event)"
            />
            <div v-if="state.busy" class="thinking" role="status">
              <span class="message-avatar"><AppIcon name="it" /></span>
              <span class="thinking-dots">
                <i></i>
                <i></i>
                <i></i>
              </span>
              <span>{{ state.streamStatus || '正在处理问题' }}</span>
            </div>
          </template>
        </div>
        <div class="composer-area">
          <form class="composer" @submit.prevent="session.send()">
            <textarea
              ref="composerInput"
              v-model="state.draft"
              rows="2"
              aria-label="员工 IT 问题"
              placeholder="输入你的问题…"
              @keydown.meta.enter.prevent="session.send()"
              @keydown.ctrl.enter.prevent="session.send()"
            >
            </textarea>
            <div class="composer-bottom">
              <span class="routing-label">
                <AppIcon name="route" />
                自动路由
              </span>
              <div class="send-controls">
                <span class="keyboard-hint">⌘ / Ctrl + Enter</span>
                <button
                  class="send-button"
                  type="submit"
                  :disabled="state.busy || !state.draft.trim()"
                  :aria-label="state.busy ? '处理中' : '发送'"
                  :title="state.busy ? '处理中' : '发送'"
                >
                  <AppIcon :name="state.busy ? 'refresh' : 'arrow'" :class="{ spinning: state.busy }" />
                </button>
              </div>
            </div>
          </form>
          <div v-if="!state.messages.length" class="prompt-grid">
            <button v-for="prompt in prompts" :key="prompt.label" @click="usePrompt(prompt.text)">
              <AppIcon :name="prompt.label === 'VPN 连不上' ? 'route' : prompt.label === '重置密码' ? 'sliders' : 'document'" />
              <span>
                <strong>{{ prompt.label }}</strong>
                <small>{{ prompt.label === 'VPN 连不上' ? '检查网络、DNS 和客户端状态' : prompt.label === '重置密码' ? '走身份平台安全重置流程' : '提交最小权限申请' }}</small>
              </span>
              <AppIcon class="prompt-arrow" name="northeast" />
            </button>
          </div>
          <p class="composer-note">AI 生成的内容可能不准确，请结合业务规则核实。</p>
          <TaskHistoryPanel v-if="identity" :client="taskHistoryClient" @open="emit('open-task', $event)" />
        </div>
      </section>
      <aside v-if="state.messages.length" class="inspector-desktop" aria-label="请求追踪与运行状态">
        <div class="inspector-heading">
          <AppIcon name="panel" />
          <h2>运行详情</h2>
        </div>
        <RequestInspector :last-response="state.lastResponse" :last-trace="state.lastTrace" :busy="state.busy" :monitor-data="monitorData" @refresh="emit('refresh-monitor')" />
      </aside>
    </div>
    <dialog ref="inspectorDialog" class="inspector-dialog" @click="closeOnBackdrop" aria-labelledby="inspector-title">
      <div class="drawer-heading">
        <div>
          <AppIcon name="panel" />
          <h2 id="inspector-title">运行详情</h2>
        </div>
        <button class="icon-button" aria-label="关闭运行详情" @click="inspectorDialog?.close()">
          <AppIcon name="close" />
        </button>
      </div>
      <RequestInspector :last-response="state.lastResponse" :last-trace="state.lastTrace" :busy="state.busy" :monitor-data="monitorData" @refresh="emit('refresh-monitor')" />
    </dialog>
  </section>
</template>

<script setup lang="ts">
// Private chat page. The session lives in App.vue so the conversation (and an in-flight reply)
// survives switching to another page and back; this component only renders it.
import { nextTick, ref, watch } from 'vue'
import AppIcon from '../components/AppIcon.vue'
import ChatMessage from '../components/ChatMessage.vue'
import RequestInspector from '../components/RequestInspector.vue'
import TaskHistoryPanel from '../components/TaskHistoryPanel.vue'
import { applyApprovalAction } from '../lib/approvalDecision.ts'
import type { ChatSession } from '../lib/chatSession.ts'
import type { createTaskClient } from '../lib/taskClient.ts'
import type { Identity, MonitorData, Settings } from '../lib/types.ts'

const props = defineProps<{
  session: ChatSession
  settings: Settings
  identity: Identity | null
  backendLabel: string
  monitorData: MonitorData
  taskHistoryClient: ReturnType<typeof createTaskClient>
}>()
const emit = defineEmits<{ 'open-task': [id: string]; 'refresh-monitor': []; toast: [message: string] }>()
const state = props.session.state

const messageList = ref<HTMLElement | null>(null)
const composerInput = ref<HTMLTextAreaElement | null>(null)
const inspectorDialog = ref<HTMLDialogElement | null>(null)
const prompts = [
  { label: 'VPN 连不上', text: 'VPN 连不上，错误码 VPN-691，应该怎么排查？' },
  { label: '重置密码', text: '我忘记了企业密码，如何安全重置？' },
  { label: '申请权限', text: '请帮我申请 GitLab 项目只读权限。' }
]

// Follow the conversation: scroll when a message is added and when a reply finishes.
watch([() => state.messages.length, () => state.busy], async () => {
  await nextTick()
  messageList.value?.scrollTo({ top: messageList.value.scrollHeight, behavior: 'smooth' })
})

function usePrompt(prompt: string) { state.draft = prompt; nextTick(() => composerInput.value?.focus()) }

async function copyMessage(content: string) {
  try {
    await navigator.clipboard.writeText(content)
    emit('toast', '回复已复制')
  } catch {
    emit('toast', '复制失败，请手动选择回复文字')
  }
}

function closeOnBackdrop(event: MouseEvent) {
  const dialog = event.currentTarget as HTMLDialogElement
  if (event.target !== dialog) return
  const rect = dialog.getBoundingClientRect()
  if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close()
}
</script>
