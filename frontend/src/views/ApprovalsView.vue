<template>
  <section class="page page-approvals">
    <div class="page-heading">
      <div class="heading-copy">
        <span class="kicker">APPROVALS</span>
        <h1>审批</h1>
        <p>审批人看到全部待审批申请，员工只看到本人的申请。Agent 起草的申请需申请人确认提交后才进入审批。批准后由执行器写入目标系统，到期自动回收。</p>
      </div>
      <button @click="loadApprovals" :disabled="loading">
        <AppIcon name="refresh" :class="{ spinning: loading }" />
        刷新
      </button>
    </div>
    <div class="approval-filters">
      <button v-for="option in approvalFilters" :key="option.id" :class="{ active: approvalFilter === option.id }" @click="approvalFilter = option.id; loadApprovals()">{{ option.label }}</button>
    </div>
    <p v-if="!approvalInbox.length" class="muted">没有{{ approvalFilters.find(o => o.id === approvalFilter)?.label }}的申请。</p>
    <ApprovalCard
      v-for="item in approvalInbox"
      :key="item.approvalId"
      :approval="item.approval"
      :busy="item.approvalBusy"
      :error="item.approvalError"
      :identity="identity"
      @action="emit('approval', item, $event)"
    />
  </section>
</template>

<script setup lang="ts">
// Keyed by connection + token in App.vue, so a new identity always gets a fresh inbox.
import { onMounted, ref } from 'vue'
import AppIcon from '../components/AppIcon.vue'
import ApprovalCard from '../components/ApprovalCard.vue'
import { listApprovals } from '../api/approvals.ts'
import { loadCurrent, type RequestState } from '../lib/requestState.ts'
import type { Approval, ApprovalDecision, ApprovalItem, Identity, Settings } from '../lib/types.ts'

const props = defineProps<{ settings: Settings; requestState: RequestState; identity: Identity | null }>()
const emit = defineEmits<{ approval: [target: ApprovalItem, decision?: ApprovalDecision]; toast: [message: string] }>()

const approvalFilters = [{ id: 'draft', label: '待我确认' }, { id: 'pending', label: '待审批' }, { id: 'approved', label: '已批准' }, { id: 'rejected', label: '已拒绝' }, { id: 'withdrawn', label: '已撤回' }]
const approvalFilter = ref('pending')
const approvalInbox = ref<ApprovalItem[]>([])
const loading = ref(false)

onMounted(loadApprovals)

async function loadApprovals() {
  if (!props.settings.accessToken) return
  loading.value = true
  await loadCurrent(props.requestState, 'approvals',
    (backend, settings) => listApprovals(backend, settings, approvalFilter.value) as Promise<{ items: Approval[] }>,
    (body, request) => {
      approvalInbox.value = body.items.map(approval => ({ approvalId: approval.approval_id, approval, approvalBackend: request.backend, approvalSettings: request.settings }))
    },
    error => emit('toast', `审批列表加载失败：${error.message}`))
  loading.value = false
}
</script>
