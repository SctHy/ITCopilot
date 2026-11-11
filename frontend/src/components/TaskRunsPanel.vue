<template>
  <details class="task-panel"><summary>运行记录与累计预算</summary>
    <p>已使用 {{ budget.used_runs }} / {{ budget.run_limit }} 次运行，已知 token {{ budget.used_tokens }} / {{ budget.token_limit }}。</p>
    <p>达到预算后暂停后续运行。正在执行的回合可能超出 token 阈值；中断或失败时的未报告消耗不计入已知 token。</p>
    <form v-if="isAdmin" @submit.prevent="act('budget', { token_limit: tokens, run_limit: runs })"><label>token 上限<input v-model.number="tokens" required type="number" min="1" max="10000000" /></label><label>运行次数上限<input v-model.number="runs" required type="number" min="1" max="10000" /></label><button :disabled="busy.budget">调整预算并继续排队</button></form>
    <article v-for="attempt in attempts" :key="attempt.id"><p>{{ labels[attempt.status] || attempt.status }} · 第 {{ attempt.attempt }} 次尝试 · 排队 {{ Math.max(0, attempt.started_at - attempt.queued_at).toFixed(1) }} 秒<span v-if="attempt.ended_at"> · 运行 {{ (attempt.ended_at - attempt.started_at).toFixed(1) }} 秒</span></p>
      <small>触发消息 {{ attempt.run_id.slice(0, 12) }}</small><p v-if="attempt.error">原因：{{ attempt.error }}</p>
      <p v-for="(tool, i) in attempt.tools" :key="i">{{ tool.tool_name }} · {{ tool.success ? '成功' : '失败' }}</p>
    </article>
  </details>
</template>
<script setup>
import { ref } from 'vue'
const props = defineProps({ budget: Object, attempts: Array, isAdmin: Boolean, busy: Object, act: Function })
const tokens = ref(props.budget.token_limit), runs = ref(props.budget.run_limit)
const labels = { running: '运行中', done: '已完成', failed: '失败', interrupted: '中断' }
</script>
