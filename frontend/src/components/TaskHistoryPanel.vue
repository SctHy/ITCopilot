<template>
  <details class="task-panel history-panel" @toggle="opened">
    <summary>{{ room ? '历史事项与关联流程' : '查找并继续历史事项' }}</summary>
    <p>只展示你当前有权访问的事项。继续处理会回到原事项，并重新读取当前状态。</p>
    <form @submit.prevent="search"><label>关键词<input v-model="state.query" maxlength="200" placeholder="例如 GitLab、VPN 或事项名称" /></label><button :disabled="state.busy">{{ state.busy ? '查询中…' : '搜索历史' }}</button></form>
    <p v-if="state.error" role="alert">{{ state.error }}</p>
    <template v-if="room && state.links.length"><h4>当前可见的关联事项</h4><p v-for="item in state.links" :key="item.id"><button @click="emit('open', item.id)">{{ item.title }}</button> · {{ statuses[item.status] }} <button v-if="canManage" @click="link(item.id, false)">取消关联</button></p></template>
    <article v-for="item in state.items" :key="item.id">
      <strong>{{ item.title }}</strong><p>{{ statuses[item.status] }} · 步骤 {{ item.steps_done }}/{{ item.steps_total }} · 未完待办 {{ item.pending_todos }} · Agent 回合 {{ item.active_runs }}</p>
      <p>{{ item.goal }}</p><p v-if="item.blocker">阻塞：{{ item.blocker }}</p><p v-if="item.next_action">下一步：{{ item.next_action }}</p><p v-if="item.resolution">历史验收：{{ item.resolution }}</p>
      <p v-for="approval in item.approvals" :key="approval.approval_id">审批 {{ approvalStatuses[approval.status] || approval.status }} · 执行 {{ executions[approval.execution] || approval.execution }}</p>
      <div class="history-actions"><button @click="emit('open', item.id)">打开原事项</button><button v-if="item.can_continue" :disabled="!!state.continuing" @click="resume(item, followup)">{{ item.status === 'open' ? '继续只读跟进' : '重新打开并只读跟进' }}</button><button v-if="room && canManage && !state.links.some(v => v.id === item.id)" @click="link(item.id, true)">关联到当前事项</button></div>
    </article>
    <p v-if="loaded && !state.busy && !state.items.length">没有匹配且可访问的历史事项。</p>
    <p v-if="room">关联不会复制讨论内容，也不会授予源事项访问权。</p>
  </details>
</template>
<script setup>
import { onBeforeUnmount, ref } from 'vue'
import { createTaskHistoryController } from '../lib/taskHistoryController.js'
const props = defineProps({ client: Object, room: Object, canManage: Boolean })
const emit = defineEmits(['open'])
const { state, search, link, resume, dispose } = createTaskHistoryController(props.client, props.room?.id, id => emit('open', id))
const loaded = ref(false), followup = '请根据当前审批、执行和任务进度继续只读跟进；需要新变更时请说明申请步骤。'
const statuses = { open: '跟进中', stopped: '已停止', resolved: '已解决' }
const approvalStatuses = { pending: '待审批', approved: '已批准', rejected: '已拒绝', expired: '已到期', superseded: '已替代', invalidated: '已失效' }
const executions = { not_executed: '未执行', queued: '排队中', executing: '执行中', succeeded: '成功', failed: '失败', manual_review: '需人工复核' }
function opened(event) { if (event.target.open) { loaded.value = true; search() } }
onBeforeUnmount(dispose)
</script>
<style scoped>
.history-panel { border: 1px solid var(--line); border-radius: 10px; padding: 14px; margin: 12px 0; }
summary { cursor: pointer; font-weight: 600; }
p { line-height: 1.65; overflow-wrap: anywhere; }
form, label { display: grid; gap: 8px; margin: 10px 0; }
article { border-bottom: 1px solid var(--line); padding: 12px 0; }
.history-actions { display: flex; flex-wrap: wrap; gap: 8px; }
</style>
