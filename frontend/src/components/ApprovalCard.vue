<template>
  <div class="approval-card">
    <strong>操作审批</strong>
    <button v-if="!approval" :disabled="busy" @click="$emit('action')">查看申请内容</button>
    <template v-else>
      <p class="approval-meta">
        申请人 {{ approval.requester_name || approval.requester }} · 策略 {{ approval.policy_version }} · 需 {{ approval.approvals_required }} 人批准
        <template v-if="approval.expires_at && approval.status === 'pending'"> · {{ formatTime(approval.expires_at) }} 前有效</template>
      </p>
      <div v-for="(action, index) in approval.action_requests" :key="index" class="approval-action">
        <b>{{ actionLabels[action.tool_name] || action.tool_name }}</b>
        <dl>
          <template v-for="(value, key) in action.arguments" :key="key">
            <dt>{{ fieldLabels[key] || key }}</dt>
            <dd>{{ value }}</dd>
          </template>
        </dl>
      </div>
      <p>
        审批：{{ statusLabels[approval.status] || approval.status }}
        <template v-if="approval.decisions?.length">（{{ approval.decisions.map(d => `${d.actor_name || d.actor} ${decisionLabels[d.decision]}`).join('，') }}）</template>
        · 执行：{{ executionLabels[approval.execution] || approval.execution }}
      </p>
      <ul v-if="approval.results?.length" class="approval-steps">
        <li v-for="step in approval.results" :key="step.operation_id">
          {{ actionLabels[step.action] || step.action }} · {{ actionStatus[step.status] || step.status }}
          <template v-if="step.adapter !== 'local'"> · {{ step.adapter }}</template>
          <template v-if="step.preexisting"> · 已有同等权限，未变更</template>
          <template v-if="step.reconciled"> · 经回查确认</template>
          <template v-if="step.error"> · {{ step.error }}</template>
        </li>
      </ul>
      <ul v-if="approval.revocations?.length" class="approval-steps">
        <li v-for="step in approval.revocations" :key="step.operation_id">
          到期回收 · {{ actionStatus[step.status] || step.status }}
          <template v-if="step.status === 'queued'"> · 计划于 {{ formatTime(step.not_before) }}</template>
          <template v-if="step.error"> · {{ step.error }}</template>
        </li>
      </ul>
      <template v-if="approval.status === 'pending' && canDecide">
        <button :disabled="busy" @click="$emit('action', 'approved')">批准</button>
        <button :disabled="busy" @click="$emit('action', 'rejected')">拒绝</button>
      </template>
      <p v-else-if="approval.status === 'pending'" class="muted">等待审批人处理；申请人不能审批自己的申请。</p>
      <button v-if="canReconcile && needsReview" :disabled="busy" @click="$emit('action', 'reconcile')">回查目标系统并对账</button>
      <button v-if="approval.status !== 'pending'" class="text-button" :disabled="busy" @click="$emit('action')">刷新状态</button>
      <details v-if="approval.results?.length">
        <summary>查看执行凭据</summary>
        <pre>{{ JSON.stringify(approval.results, null, 2) }}</pre>
      </details>
    </template>
    <p v-if="error" role="alert">{{ error }}</p>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  approval: { type: Object, default: null },
  busy: { type: Boolean, default: false },
  error: { type: String, default: '' },
  identity: { type: Object, default: null },
})
defineEmits(['action'])

const roles = computed(() => props.identity?.roles || [])
// 只用于隐藏按钮；职责分离由服务端强制执行。
const canDecide = computed(() => roles.value.some(r => r === 'approver' || r === 'it_admin') && props.approval?.requester !== props.identity?.subject)
const canReconcile = computed(() => roles.value.includes('it_admin'))
const needsReview = computed(() => [...(props.approval?.results || []), ...(props.approval?.revocations || [])].some(s => ['manual_review', 'unknown'].includes(s.status)))

const actionLabels = { request_access: '申请权限', reset_password: '重置密码', unlock_account: '解锁账号', create_ticket: '创建工单', revoke_access: '回收权限' }
const fieldLabels = { employee_id: '工号', system: '系统', role: '角色', reason: '理由', title: '标题', description: '描述', priority: '优先级', duration_hours: '有效期（小时）' }
const statusLabels = { pending: '待审批', approved: '已批准', rejected: '已拒绝', expired: '已过期', superseded: '已被替代' }
const decisionLabels = { approved: '批准', rejected: '拒绝' }
const executionLabels = { not_executed: '尚未执行', queued: '排队中', executing: '执行中', succeeded: '执行成功', failed: '执行失败（已补偿）', manual_review: '需人工复核' }
const actionStatus = { queued: '排队中', executing: '执行中', unknown: '结果未知，待回查', succeeded: '成功', failed: '失败', cancelled: '已取消', manual_review: '需人工复核' }

function formatTime(iso) {
  return iso ? new Date(iso).toLocaleString('zh-CN', { hour12: false }) : ''
}
</script>
