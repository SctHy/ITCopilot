<template>
  <article :class="['message', item.role, { 'message-error': item.error }]">
    <span v-if="item.role === 'assistant'" class="message-avatar"><AppIcon name="it" /></span>
    <div class="message-content">
      <div class="message-meta">
        <strong>{{ item.role === 'user' ? '你' : 'IT-Copilot' }}</strong>
        <span v-if="item.error" class="error-label">请求失败</span>
        <small v-else-if="item.role === 'assistant'">{{ backendLabel }} Agent</small>
      </div>
      <span v-if="item.meta && !item.error" class="agent-tag">
        <AppIcon name="route" />
        {{ item.meta }}
      </span>
      <p>{{ formatBody(item.content) }}</p>
      <p v-if="item.taskReferences?.length">参考的本人事项：<button v-for="source in item.taskReferences" :key="source.id" class="quiet-button" @click="emit('open-task', source.id)">{{ source.title }} · 打开原流程</button></p>
      <ApprovalCard
        v-if="item.approvalId"
        :approval="item.approval"
        :busy="item.approvalBusy"
        :error="item.approvalError"
        :identity="identity"
        @action="emit('approval', item as ApprovalItem, $event)"
      />
      <div v-for="related in item.relatedApprovals || []" :key="related.approvalId" class="related-approval">
        <p class="muted">{{ related.kind === 'confirm' ? '待你确认提交的申请' : related.kind === 'withdraw' ? '可撤回的申请' : '可提前归还的权限' }}：{{ related.summary }}</p>
        <ApprovalCard
          :approval="related.approval"
          :busy="related.approvalBusy"
          :error="related.approvalError"
          :identity="identity"
          @action="emit('approval', related, $event)"
        />
      </div>
      <details v-if="item.trace?.toolCalls?.length || item.trace?.toolsUsed?.length" class="message-trace">
        <summary>
          <AppIcon name="chevron" />
          <span>工具调用</span>
          <span class="count-tag">{{ item.trace.toolCalls?.length || item.trace.toolsUsed?.length || 0 }}</span>
        </summary>
        <div v-for="(call, index) in item.trace.toolCalls" :key="`${item.id}-${index}`" class="trace-call-item">
          <div class="trace-call-meta">
            <strong>{{ call.tool_name || 'unknown_tool' }}</strong>
            <span :class="call.success ? 'success' : 'danger'">{{ call.success ? '成功' : '失败' }}</span>
          </div>
          <pre>{{ formatJson(call.input || {}) }}</pre>
          <p v-if="!call.success && call.error" class="trace-call-error">{{ call.error }}</p>
        </div>
        <p v-if="!item.trace.toolCalls?.length" class="muted">已调用：{{ item.trace.toolsUsed?.join(' · ') }}</p>
      </details>
      <button
        v-if="item.role === 'assistant' && !item.error"
        class="icon-button copy-button"
        @click="emit('copy', item.content)"
        aria-label="复制回复"
        title="复制回复"
      >
        <AppIcon name="copy" />
      </button>
    </div>
  </article>
</template>

<script setup lang="ts">
import AppIcon from './AppIcon.vue'
import ApprovalCard from './ApprovalCard.vue'
import { formatBody, formatJson } from '../lib/format.ts'
import type { ApprovalDecision, ApprovalItem, ChatMessage, Identity } from '../lib/types.ts'

defineProps<{ item: ChatMessage; identity: Identity | null; backendLabel: string }>()
const emit = defineEmits<{
  approval: [target: ApprovalItem, decision?: ApprovalDecision]
  copy: [content: string]
  'open-task': [id: string]
}>()
</script>
