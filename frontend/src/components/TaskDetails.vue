<template>
  <section class="task-details" aria-label="任务协作与运行">
    <p v-if="state.error" role="alert">{{ state.error }}</p>
    <template v-if="state.data">
      <TaskHistoryPanel :room="room" :client="client" :can-manage="canManage" @open="emit('open', $event)" />
      <TaskSuggestionsPanel :suggestions="state.data.suggestions || []" :version="state.data.progress.version" :room="room" :busy="state.busy" :act="act" />
      <TaskProgressPanel :progress="state.data.progress" :room="room" :can-manage="canManage" :busy="state.busy" :act="act" />
      <TaskVerificationPanel v-if="state.data.verification" :verification="state.data.verification" :checkpoints="state.data.checkpoints || []" :room="room" :can-manage="canManage" :busy="state.busy" :act="act" />
      <TaskTodoPanel ref="todos" :members="state.data.members || []" :reviewers="state.data.reviewers || []" :todos="state.data.todos" :room="room" :identity="identity" :busy="state.busy" :act="act" />
      <TaskReminderPanel :reminders="state.data.reminders" :room="room" :can-manage="canManage" :busy="state.busy" :act="act" />
      <TaskRunsPanel :budget="state.data.budget" :attempts="state.data.attempts" :is-admin="isAdmin" :busy="state.busy" :act="act" />
      <TaskFilesPanel :files="state.data.files || []" :room="room" :client="client" @changed="changed" />
      <TaskMembersPanel :members="state.data.members || []" :reviewers="state.data.reviewers || []" :room="room" :client="client" :can-manage="canManage" :is-admin="isAdmin" :busy="state.busy" :act="act" />
      <TaskNotificationsPanel v-if="state.data.notifications" :notifications="state.data.notifications" :members="state.data.members || []" :can-manage="canManage" :busy="state.busy" :act="act" />
    </template>
  </section>
</template>
<script setup lang="ts">
import { computed, nextTick, onMounted, onBeforeUnmount, ref } from 'vue'
import TaskSuggestionsPanel from './TaskSuggestionsPanel.vue'
import TaskHistoryPanel from './TaskHistoryPanel.vue'
import TaskFilesPanel from './TaskFilesPanel.vue'
import TaskMembersPanel from './TaskMembersPanel.vue'
import TaskNotificationsPanel from './TaskNotificationsPanel.vue'
import TaskProgressPanel from './TaskProgressPanel.vue'
import TaskVerificationPanel from './TaskVerificationPanel.vue'
import TaskTodoPanel from './TaskTodoPanel.vue'
import TaskReminderPanel from './TaskReminderPanel.vue'
import TaskRunsPanel from './TaskRunsPanel.vue'
import { createTaskDetailsController } from '../lib/taskDetailsController.ts'
import { createPolling } from '../lib/stateMachine.ts'
import type { TaskClient } from '../lib/taskClient.ts'
import type { Approval, Identity, TaskRoom } from '../lib/types.ts'
const props = defineProps<{ room: TaskRoom; identity: Identity | null; client: TaskClient }>()
const emit = defineEmits<{ changed: []; open: [id: string] }>()
const { state, refresh, perform, dispose } = createTaskDetailsController(props.client, props.room.id)
const todos = ref<InstanceType<typeof TaskTodoPanel> | null>(null)
const canManage = computed(() => props.room.requester === props.identity?.subject || !!props.identity?.roles?.includes('it_admin'))
const isAdmin = computed(() => !!props.identity?.roles?.includes('it_admin'))
const polling = createPolling(refresh, 2000)
async function changed() { await refresh(); emit('changed') }
async function act(action: string, payload?: Record<string, unknown>) { const ok = await perform(action, payload); if (ok) emit('changed'); return ok }
async function clarify(approval: Approval) { if (!todos.value) { await refresh(); await nextTick() }; todos.value?.clarify(approval) }
defineExpose({ clarify })
onMounted(() => { refresh(); polling.start() })
onBeforeUnmount(() => { dispose(); polling.stop() })
</script>
<style scoped>
.task-details { margin: 20px 0; display: grid; gap: 12px; }
.task-details :deep(.task-panel) { border: 1px solid var(--line); border-radius: 10px; padding: 14px; }
.task-details :deep(summary) { cursor: pointer; font-weight: 600; }
.task-details :deep(form), .task-details :deep(label), .task-details :deep(.step-editor) { display: grid; gap: 8px; margin: 12px 0; }
.task-details :deep(input), .task-details :deep(textarea), .task-details :deep(select) { max-width: 100%; }
.task-details :deep(input[type="checkbox"]) { width: auto; justify-self: start; }
.task-details :deep(p) { line-height: 1.7; overflow-wrap: anywhere; }
.task-details :deep(article) { border-bottom: 1px solid var(--line); padding: 8px 0; }
</style>
