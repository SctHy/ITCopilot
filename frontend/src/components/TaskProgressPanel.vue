<template>
  <details open class="task-panel"><summary>任务进度与验收</summary>
    <p>{{ progress.goal }}</p><p v-if="progress.blocker">阻塞：{{ progress.blocker }}</p><p v-if="progress.next_action">下一步：{{ progress.next_action }}</p>
    <ol><li v-for="(step, i) in progress.steps" :key="i">{{ labels[step.status] }} · {{ step.title }}<small>（{{ step.evidence_ids.length }} 条证据）</small></li></ol>
    <p v-if="progress.resolution">验收说明：{{ progress.resolution }}</p>
    <button v-if="room.status === 'open' && room.can_write !== false && !draft" @click="edit">编辑进度</button>
    <form v-if="draft" @submit.prevent="save">
      <label>目标<input v-model="draft.goal" required maxlength="8000" /></label>
      <div v-for="(step, i) in draft.steps" :key="i" class="step-editor">
        <input v-model="step.title" required maxlength="200" placeholder="步骤与完成标准" />
        <select v-model="step.status"><option value="todo">待处理</option><option value="working">进行中</option><option value="done">已完成</option></select>
        <label>证据消息（可多选）<select v-model="step.evidence_ids" multiple><option v-for="event in room.events" :key="event.id" :value="event.id">{{ event.actor_name }}：{{ event.text.slice(0, 70) }}</option></select></label>
        <button type="button" @click="draft.steps.splice(i, 1)">删除步骤</button>
      </div>
      <button type="button" :disabled="draft.steps.length >= 30" @click="draft.steps.push({ title: '', status: 'todo', evidence_ids: [] })">添加步骤</button>
      <label>阻塞原因<input v-model="draft.blocker" maxlength="4000" /></label><label>下一步<input v-model="draft.next_action" maxlength="4000" /></label>
      <button :disabled="busy.progress">保存进度</button><button type="button" @click="draft = null">取消</button>
    </form>
    <form v-if="canManage && room.status === 'open'" @submit.prevent="act('lifecycle', { status: 'resolved', resolution })">
      <label>验收说明<input v-model="resolution" required maxlength="4000" placeholder="说明如何验证目标已完成" /></label><button :disabled="busy.lifecycle">验收解决</button>
    </form>
    <button v-if="canManage && room.status !== 'open'" :disabled="busy.lifecycle" @click="act('lifecycle', { status: 'open' })">重新打开</button>
  </details>
</template>
<script setup lang="ts">
import { ref } from 'vue'
import type { TaskAct } from '../lib/taskDetailsController.ts'
import type { TaskProgress, TaskRoom } from '../lib/types.ts'
const props = defineProps<{ progress: TaskProgress; room: TaskRoom; canManage: boolean; busy: Record<string, boolean>; act: TaskAct }>()
const draft = ref<TaskProgress | null>(null), resolution = ref(''), labels: Record<string, string> = { todo: '待处理', working: '进行中', done: '已完成' }
function edit() { draft.value = JSON.parse(JSON.stringify(props.progress)) }
async function save() { if (draft.value && await props.act('progress', { ...draft.value })) draft.value = null }
</script>
