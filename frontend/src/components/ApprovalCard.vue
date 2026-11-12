<template>
  <div class="approval-card" :data-state="viewState">
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
        <template v-if="approval.decisions?.length">（{{ approval.decisions.map(d => `${d.actor_name || d.actor} ${decisionLabels[d.decision] ?? d.decision}`).join('，') }}）</template>
        · 执行：{{ executionLabels[approval.execution ?? ''] || approval.execution }}
        <template v-if="lifecycleLabels[approval.lifecycle ?? '']"> · {{ lifecycleLabels[approval.lifecycle ?? ''] }}</template>
      </p>
      <ul v-if="approval.results?.length" class="approval-steps">
        <li v-for="step in approval.results" :key="step.operation_id">
          {{ actionLabels[step.action ?? ''] || step.action }} · {{ actionStatus[step.status] || step.status }}
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
      <ApprovalActions :approval="approval" :identity="identity" :busy="busy" :allow-clarification="allowClarification" @action="$emit('action', $event)" />
      <details v-if="approval.results?.length">
        <summary>查看执行凭据</summary>
        <pre>{{ JSON.stringify(approval.results, null, 2) }}</pre>
      </details>
    </template>
    <p v-if="error" role="alert">{{ error }}</p>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { resourceState } from '../lib/stateMachine.ts'
import ApprovalActions from './ApprovalActions.vue'
import type { Approval, ApprovalDecision, Identity } from '../lib/types.ts'

const props = withDefaults(defineProps<{
  approval?: Approval | null
  busy?: boolean
  error?: string
  identity?: Identity | null
  allowClarification?: boolean
}>(), { approval: null, busy: false, error: '', identity: null, allowClarification: false })
// No decision means "load the approval" (the card starts empty).
defineEmits<{ action: [decision?: ApprovalDecision] }>()

const viewState = computed(() => resourceState({ pending: props.busy, error: props.error, loaded: !!props.approval }))

const actionLabels: Record<string, string> = { request_access: '申请权限', reset_password: '重置密码', unlock_account: '解锁账号', create_ticket: '创建工单', revoke_access: '回收权限', lock_device: '远程锁定设备' }
const fieldLabels: Record<string, string> = { employee_id: '工号', system: '系统', role: '角色', reason: '理由', title: '标题', description: '描述', priority: '优先级', duration_hours: '有效期（小时）', device_id: '设备' }
const statusLabels: Record<string, string> = { pending: '待审批', approved: '已批准', rejected: '已拒绝', expired: '已过期', superseded: '已被替代', invalidated: '策略变更已失效', withdrawn: '申请人已撤回' }
const decisionLabels: Record<string, string> = { approved: '批准', rejected: '拒绝' }
const executionLabels: Record<string, string> = { not_executed: '尚未执行', queued: '排队中', executing: '执行中', succeeded: '执行成功', failed: '执行失败（请查看回收状态）', manual_review: '需人工复核' }
const lifecycleLabels: Record<string, string> = { compensating: '正在补偿回收', active_until_revocation: '权限生效中，已安排回收', revoking: '正在回收权限', manual_review: '业务流程需人工复核', expiry_due: '已到期，等待过期扫描' }
const actionStatus: Record<string, string> = { queued: '排队中', executing: '执行中', unknown: '结果未知，待回查', succeeded: '成功', failed: '失败', cancelled: '已取消', manual_review: '需人工复核' }

function formatTime(iso?: string | null) {
  return iso ? new Date(iso).toLocaleString('zh-CN', { hour12: false }) : ''
}
</script>
