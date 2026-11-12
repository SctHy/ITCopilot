<template>
  <details open class="task-panel"><summary>协作待办</summary>
    <article v-for="todo in todos" :key="todo.id"><p>{{ todo.question }} · {{ names[todo.assignee] || todo.assignee }} · {{ todo.status === 'open' ? '待答复' : todo.status === 'cancelled' ? '已取消' : '已答复' }}</p>
      <small v-if="todo.due_at">截止 {{ new Date(todo.due_at * 1000).toLocaleString() }}</small><p v-if="todo.answer">{{ todo.answer }}</p>
      <form v-if="todo.status === 'open' && todo.assignee === identity?.subject && room.status === 'open'" @submit.prevent="answer(todo)">
        <textarea v-model="answers[todo.id]" required maxlength="8000" placeholder="补充资料或回答问题" /><button :disabled="busy['todos/' + todo.id + '/answer']">提交答复并请 Agent 跟进</button>
      </form>
      <form v-if="todo.status === 'open' && room.can_write !== false && (todo.creator === identity?.subject || identity?.roles?.includes('it_admin'))" @submit.prevent="change(todo)">
        <select v-model="changes[todo.id].action"><option value="cancel">取消待办</option><option value="reassign">改派待办</option></select>
        <select v-if="changes[todo.id].action === 'reassign'" v-model="changes[todo.id].assignee" required><option v-for="(name,id) in names" :key="id" :value="id">{{ name }}</option></select>
        <input v-model="changes[todo.id].reason" required maxlength="8000" placeholder="取消或改派原因" /><button :disabled="busy['todos/' + todo.id + '/change']">提交调整</button>
      </form>
    </article>
    <form v-if="room.status === 'open' && room.can_write !== false" @submit.prevent="create">
      <label>问题<textarea ref="questionInput" v-model="question" required maxlength="8000" /></label>
      <label>回答人<select v-model="assignee" required><option v-for="(name, id) in names" :key="id" :value="id">{{ name }}</option></select></label>
      <label>截止时间（可选）<input v-model="due" type="datetime-local" /></label><button :disabled="busy.todos">发起补充待办</button>
    </form>
  </details>
</template>
<script setup lang="ts">
import { computed, nextTick, ref, watchEffect } from 'vue'
import type { TaskAct } from '../lib/taskDetailsController.ts'
import type { Approval, Identity, TaskMember, TaskRoom, TaskTodo } from '../lib/types.ts'
const props = defineProps<{ todos: TaskTodo[]; members: TaskMember[]; reviewers: string[]; room: TaskRoom; identity: Identity | null; busy: Record<string, boolean>; act: TaskAct }>()
const question = ref(''), due = ref(''), assignee = ref(props.room.requester), answers = ref<Record<string, string>>({}), questionInput = ref<HTMLTextAreaElement | null>(null)
const changes = ref<Record<string, { action: 'cancel' | 'reassign'; assignee: string; reason: string }>>({})
watchEffect(() => { for (const todo of props.todos) changes.value[todo.id] ||= { action: 'cancel', assignee: props.room.requester, reason: '' } })
async function change(todo: TaskTodo) { await props.act('todos/' + todo.id + '/change', { ...changes.value[todo.id] }) }
const names = computed(() => {
  const result: Record<string, string> = { [props.room.requester]: props.room.requester_name }
  if (props.room.can_write !== false && props.identity?.subject) result[props.identity.subject] = props.identity.username || '我'
  const eventNames: Record<string, string> = Object.fromEntries(props.room.events.map(e => [e.actor, e.actor_name]))
  for (const id of props.reviewers || []) result[id] = eventNames[id] || id
  for (const member of props.members || []) { if (member.role === 'collaborator') result[member.subject] = member.profile.username; else delete result[member.subject] }
  return result
})
async function create() {
  if (await props.act('todos', { question: question.value, assignee: assignee.value, due_at: due.value ? new Date(due.value).getTime() / 1000 : null })) { question.value = ''; due.value = '' }
}
async function answer(todo: TaskTodo) { if (await props.act('todos/' + todo.id + '/answer', { answer: answers.value[todo.id] })) answers.value[todo.id] = '' }
async function clarify(approval: Approval) { question.value = `关于申请 ${approval.approval_id.slice(0, 12)}，请补充说明：`; assignee.value = props.room.requester; await nextTick(); questionInput.value?.focus() }
defineExpose({ clarify })
</script>
