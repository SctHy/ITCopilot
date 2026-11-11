<template>
  <details class="task-panel" open><summary>Agent 进度建议</summary>
    <p v-if="!suggestions.length">Agent 回复后会尝试整理步骤、阻塞点和下一步建议。</p>
    <article v-for="item in suggestions" :key="item.id"><p>{{ item.payload.goal }} · {{ labels[item.status] }}</p>
      <ol><li v-for="(step, i) in item.payload.steps" :key="i">{{ stages[step.status] }} · {{ step.title }}（{{ step.evidence_ids.length }} 条证据）</li></ol>
      <p v-if="item.payload.blocker">阻塞：{{ item.payload.blocker }}</p><p>下一步：{{ item.payload.next_action }}</p>
      <template v-if="item.status === 'pending' && room.can_write !== false && room.status === 'open'">
        <p v-if="item.base_version !== version">人工进度已更新，此建议不能直接覆盖，可参考后编辑进度。</p>
        <button :disabled="busy['suggestions/' + item.id] || item.base_version !== version" @click="act('suggestions/' + item.id, { accept: true })">采纳进度建议</button>
        <button :disabled="busy['suggestions/' + item.id]" @click="act('suggestions/' + item.id, { accept: false })">忽略</button>
      </template>
    </article>
  </details>
</template>
<script setup>
defineProps({ suggestions: Array, version: Number, room: Object, busy: Object, act: Function })
const labels = { pending: '待处理', accepted: '已采纳', dismissed: '已忽略' }, stages = { todo: '待处理', working: '进行中', done: '有完成证据' }
</script>
