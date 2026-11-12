<template>
  <dialog ref="dialog" class="connection-dialog" @click="closeOnBackdrop" aria-labelledby="connection-title">
    <div class="dialog-heading">
      <div>
        <span class="kicker">CONNECTION</span>
        <h2 id="connection-title">连接配置</h2>
      </div>
      <button class="icon-button" aria-label="关闭连接配置" @click="dialog?.close()">
        <AppIcon name="close" />
      </button>
    </div>
    <p class="dialog-description">配置 {{ backendLabel }} 服务与当前会话。</p>
    <form @submit.prevent="save">
      <label>
        <span>服务地址</span>
        <input v-model="draft.endpoint" required placeholder="http://127.0.0.1:8001" />
        <small>使用代理路径或完整的 HTTP / HTTPS 地址。</small>
      </label>
      <div class="identity-block">
        <span>当前身份</span>
        <strong>{{ auth.state.identity ? `${auth.state.identity.username} · 工号 ${auth.state.identity.employee_id || '-'} · ${auth.state.identity.department}` : '未登录' }}</strong>
        <small v-if="auth.state.identity">角色：{{ formatRoles(auth.state.identity.roles) || '无' }}（来自已验证令牌，服务端强制执行）</small>
        <template v-if="auth.state.config?.mode === 'dev'">
          <select v-model="auth.state.devUser" aria-label="演示身份">
            <option v-for="user in auth.state.config.users" :key="user.username" :value="user.username">
              {{ user.username }} · {{ user.department }} · {{ formatRoles(user.roles) }}
            </option>
          </select>
          <button type="button" class="quiet-button" @click="switchDevUser">切换到该演示身份</button>
        </template>
        <template v-else-if="auth.state.config?.mode === 'oidc'">
          <button v-if="!auth.state.identity" type="button" @click="auth.loginOidc()">通过 Keycloak 登录</button>
          <button v-else type="button" class="quiet-button" @click="auth.logoutOidc()">退出登录</button>
        </template>
      </div>
      <label>
        <span>会话 ID</span>
        <input v-model="draft.conversationId" placeholder="留空以自动生成" />
      </label>
      <div class="dialog-health">
        <i :class="health.ok ? 'online' : 'offline'"></i>
        <span>{{ health.ok ? '服务已连接' : health.label }}</span>
        <button type="button" class="text-button" @click="emit('check-health')">
          检查当前连接
          <AppIcon name="refresh" />
        </button>
      </div>
      <details v-if="health.detail" class="connection-detail">
        <summary>查看连接详情</summary>
        <pre>{{ health.detail }}</pre>
      </details>
      <div class="dialog-actions">
        <button type="button" class="quiet-button" @click="dialog?.close()">取消</button>
        <button type="submit">
          保存配置
          <AppIcon name="check" />
        </button>
      </div>
    </form>
  </dialog>
</template>

<script setup lang="ts">
// Connection settings and sign-in. App.vue applies a saved connection; this dialog only edits a draft.
import { reactive, ref } from 'vue'
import AppIcon from './AppIcon.vue'
import { formatRoles } from '../lib/format.ts'
import type { AuthSession } from '../lib/authSession.ts'

export interface ConnectionDraft { endpoint: string; conversationId: string }
export interface HealthState { ok: boolean; label: string; detail: string }

const props = defineProps<{ backendLabel: string; auth: AuthSession; health: HealthState }>()
const emit = defineEmits<{ save: [draft: ConnectionDraft]; 'check-health': []; toast: [message: string] }>()

const dialog = ref<HTMLDialogElement | null>(null)
const draft = reactive<ConnectionDraft>({ endpoint: '', conversationId: '' })

function open(current: ConnectionDraft) {
  Object.assign(draft, current)
  dialog.value?.showModal()
}
defineExpose({ open })

function save() {
  const endpoint = draft.endpoint.trim().replace(/\/+$/, '')
  if (!endpoint || !(endpoint.startsWith('/') || /^https?:\/\//i.test(endpoint))) {
    emit('toast', '请输入代理路径或 HTTP / HTTPS 服务地址')
    return
  }
  emit('save', { endpoint, conversationId: draft.conversationId.trim() })
  dialog.value?.close()
}

async function switchDevUser() {
  try {
    await props.auth.loginAs(props.auth.state.devUser)
  } catch (error) {
    emit('toast', `切换身份失败：${error instanceof Error ? error.message : String(error)}`)
  }
}

function closeOnBackdrop(event: MouseEvent) {
  const target = event.currentTarget as HTMLDialogElement
  if (event.target !== target) return
  const rect = target.getBoundingClientRect()
  if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) target.close()
}
</script>
