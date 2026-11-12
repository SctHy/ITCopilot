<template>
  <div class="approval-options" role="group" aria-label="审批选项">
    <template v-if="approval?.status === 'draft'">
      <p class="muted">这是 Agent 起草的申请，核对上面的内容无误后再提交；提交后才会交给审批人。</p>
      <button v-if="permissions.canConfirm" type="button" :disabled="busy" @click="$emit('action', 'confirm')">确认提交</button>
      <button v-if="permissions.canWithdraw" type="button" class="quiet-button" :disabled="busy" @click="$emit('action', 'withdraw')">取消申请</button>
      <p v-if="permissions.reason" class="muted">{{ permissions.reason }}</p>
    </template>
    <template v-if="approval?.status === 'pending'">
      <button v-if="permissions.canDecide" type="button" :disabled="busy" @click="$emit('action', 'approved')">同意审批</button>
      <button v-if="permissions.canDecide" type="button" :disabled="busy" @click="$emit('action', 'rejected')">拒绝申请</button>
      <button v-if="allowClarification && permissions.canClarify" type="button" class="quiet-button" :disabled="busy" @click="$emit('action', 'clarify')">要求补充说明</button>
      <button v-if="permissions.canWithdraw" type="button" class="quiet-button" :disabled="busy" @click="confirmThen('withdraw', '确认撤回这条申请？撤回后不会再执行，如仍需要请重新提交。')">撤回申请</button>
      <p v-if="permissions.reason && !permissions.canWithdraw" class="muted">{{ permissions.reason }}</p>
    </template>
    <button v-if="permissions.canRelease" type="button" class="quiet-button" :disabled="busy" @click="confirmThen('release', '确认现在归还这项限时权限？归还后如需使用要重新申请。')">提前归还权限</button>
    <button v-if="permissions.canReconcile && needsReview" type="button" :disabled="busy" @click="$emit('action', 'reconcile')">回查目标系统并对账</button>
    <button v-if="permissions.canReconcile && manualReview" type="button" class="quiet-button" :disabled="busy" @click="resolve">已人工处理，结案</button>
    <button type="button" class="text-button" :disabled="busy" @click="$emit('action')">刷新状态</button>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { approvalPermissions } from '../lib/approvalActions.ts'
import type { Approval, ApprovalDecision, Identity } from '../lib/types.ts'

const props = defineProps<{ approval: Approval | null; identity: Identity | null; busy?: boolean; allowClarification?: boolean }>()
// No decision means "reload this approval".
const emit = defineEmits<{ action: [decision?: ApprovalDecision, note?: string] }>()
const permissions = computed(() => approvalPermissions(props.approval, props.identity))
function confirmThen(action: ApprovalDecision, message: string) {
  if (typeof window === 'undefined' || window.confirm(message)) emit('action', action)
}
const steps = computed(() => [...(props.approval?.results || []), ...(props.approval?.revocations || [])])
const manualReview = computed(() => steps.value.some(step => step.status === 'manual_review'))
// 回查无法结束的复核（有人有意改了权限）只能人工结案；说明进入审批审计。
function resolve() {
  const note = typeof window === 'undefined' ? '' : (window.prompt('说明在目标系统中的处理结果（必填，进入审计）') || '').trim()
  if (note) emit('action', 'resolve', note)
}
const needsReview = computed(() => [...(props.approval?.results || []), ...(props.approval?.revocations || [])].some(step => ['manual_review', 'unknown'].includes(step.status)))
</script>

<style scoped>
.approval-options { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-top: 12px; }
.approval-options p { width: 100%; }
</style>
