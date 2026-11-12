<template>
  <details class="task-panel"><summary>定时提醒与复查</summary>
    <p>到期消息保存在本事项中；勾选复查后会唤醒 Agent 做只读跟进。</p>
    <p v-for="timer in reminders" :key="timer.id">{{ timer.text }} · {{ new Date(timer.due_at * 1000).toLocaleString() }} · {{ labels[timer.status] }}
      <button v-if="canManage && timer.status === 'pending'" :disabled="busy['reminders/' + timer.id + '/cancel']" @click="act('reminders/' + timer.id + '/cancel', {})">取消</button>
    </p>
    <form v-if="canManage && room.status === 'open'" @submit.prevent="create"><input v-model="text" required maxlength="8000" placeholder="提醒内容或复查目标" /><input v-model="due" required type="datetime-local" />
      <label><input v-model="wake" type="checkbox" />到期请 Agent 只读复查</label><button :disabled="busy.reminders">安排提醒</button>
    </form>
  </details>
</template>
<script setup lang="ts">
import { ref } from 'vue'
import type { TaskAct } from '../lib/taskDetailsController.ts'
import type { TaskReminder, TaskRoom } from '../lib/types.ts'
const props = defineProps<{ reminders: TaskReminder[]; room: TaskRoom; canManage: boolean; busy: Record<string, boolean>; act: TaskAct }>()
const text = ref(''), due = ref(''), wake = ref(false), labels: Record<string, string> = { pending: '待触发', fired: '已触发', cancelled: '已取消' }
async function create() { if (await props.act('reminders', { text: text.value, due_at: new Date(due.value).getTime() / 1000, wake: wake.value })) { text.value = ''; due.value = '' } }
</script>
