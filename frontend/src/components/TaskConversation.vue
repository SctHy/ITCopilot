<template>
  <div class="task-transcript" role="log" aria-label="共享事项讨论" aria-live="polite">
    <article v-for="event in room.events" :key="event.id" :class="['task-event', event.kind]">
      <div class="event-meta"><strong>{{ event.actor_name }}</strong><span>{{ roles[event.kind] || '系统' }}</span><time>{{ formatTime(event.created_at) }}</time></div>
      <p>{{ event.text }}</p>
      <ApprovalCard v-if="linked[event.id]" :approval="linked[event.id]" :identity="identity"
        :busy="!!approvalBusy[linked[event.id].approval_id]" :error="approvalErrors[linked[event.id].approval_id] || ''"
        :allow-clarification="room.status === 'open'" @action="$emit('approval', linked[event.id], $event)" />
    </article>
    <section v-if="unlinked.length" aria-label="其他事项审批">
      <ApprovalCard v-for="approval in unlinked" :key="approval.approval_id" :approval="approval" :identity="identity"
        :busy="!!approvalBusy[approval.approval_id]" :error="approvalErrors[approval.approval_id] || ''"
        :allow-clarification="room.status === 'open'" @action="$emit('approval', approval, $event)" />
    </section>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import ApprovalCard from './ApprovalCard.vue'
const props = defineProps({ room: { type: Object, required: true }, identity: Object,
  approvalBusy: { type: Object, default: () => ({}) }, approvalErrors: { type: Object, default: () => ({}) } })
defineEmits(['approval'])
const linked = computed(() => {
  const used = new Set(), out = {}
  for (const event of props.room.events) {
    if (event.kind !== 'assistant') continue
    const approval = props.room.approvals?.find(item => item.approval_id === event.data?.approval_id)
    if (approval && !used.has(approval.approval_id)) { out[event.id] = approval; used.add(approval.approval_id) }
  }
  return out
})
const unlinked = computed(() => (props.room.approvals || []).filter(item => !Object.values(linked.value).some(a => a.approval_id === item.approval_id)))
const roles = { requester: '申请人', approver: '审批人 / IT 支持', collaborator: '协作者', assistant: 'Agent', approval: '审批状态', approval_vote: '正式审批决定', system: '系统', progress: '任务进度', todo: '协作待办', todo_answer: '待办答复', todo_changed: '待办调整', reminder: '定时提醒', approval_reminder: '审批提醒', budget: '运行预算', file: '附件材料', member: '成员管理', reviewer_assignment: '审批指派', suggestion: '进度建议' }
function formatTime(seconds) { return new Date(seconds * 1000).toLocaleString('zh-CN', { hour12: false }) }
</script>

<style scoped>
.task-transcript { display: grid; gap: 12px; margin: 24px 0; }
.task-event { padding: 16px; border: 1px solid var(--line); border-radius: 12px; }
.event-meta { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
.task-event p { white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.8; margin-bottom: 0; }
.task-event.assistant { background: var(--soft); }
.task-event.system, .task-event.approval, .task-event.approval_vote { border-style: dashed; }
.task-event time, .task-event span { opacity: .65; font-size: 12px; }
</style>
