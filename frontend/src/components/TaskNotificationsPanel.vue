<template>
  <details class="task-panel"><summary>站外通知与超时升级</summary>
    <p v-if="!notifications.channels.length">管理员尚未为当前身份配置邮件或飞书通知地址。</p>
    <form v-if="notifications.channels.length" @submit.prevent="act('notifications', { channel, enabled, escalate_to: escalation || null })">
      <label><input v-model="enabled" type="checkbox" />订阅事项更新</label><select v-model="channel"><option v-for="name in notifications.channels" :key="name" :value="name">{{ name === 'email' ? '邮件' : '飞书' }}</option></select>
      <label v-if="canManage">超时升级给（选填）<select v-model="escalation"><option value="">不升级</option><option v-for="person in members.filter(m => m.role !== 'revoked')" :key="person.subject" :value="person.subject">{{ person.profile.username }}</option></select></label>
      <p>仅发送订阅后的更新；到期事件可通知升级对象。对方需已配置同一通知渠道。</p><button :disabled="busy.notifications">保存通知设置</button>
    </form>
    <p v-for="delivery in notifications.deliveries" :key="delivery.id">{{ delivery.channel }} · {{ labels[delivery.status] }} · 尝试 {{ delivery.attempts }} 次 <span v-if="delivery.error">（{{ delivery.error }}）</span></p>
  </details>
</template>
<script setup lang="ts">
import { ref } from 'vue'
import type { TaskAct } from '../lib/taskDetailsController.ts'
import type { TaskMember, TaskNotifications } from '../lib/types.ts'
const props = defineProps<{ notifications: TaskNotifications; members: TaskMember[]; canManage: boolean; busy: Record<string, boolean>; act: TaskAct }>()
const channel = ref(props.notifications.subscription?.channel || props.notifications.channels[0] || 'email'), enabled = ref(!!props.notifications.subscription?.enabled), escalation = ref(props.notifications.subscription?.escalate_to || '')
const labels: Record<string, string> = { queued: '等待投递', sending: '投递中', sent: '已投递', failed: '投递失败', cancelled: '已取消' }
</script>
