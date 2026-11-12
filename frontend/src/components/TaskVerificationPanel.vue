<template>
  <details open class="task-panel"><summary>目标独立核验</summary>
    <p>核验申请人的当前系统权限。核验通过后，由申请人验收事项；登录和实际访问仍需另行确认。</p>
    <ul><li v-for="check in verification.goal.checks" :key="check.system + check.role">{{ check.system }} 权限至少达到 {{ check.role }}</li></ul>
    <p v-if="!verification.goal.checks.length">尚未配置自动核验条件，使用人工验收。</p>
    <p v-else>自动只读跟进 {{ verification.goal.replans_used }} / {{ verification.goal.replan_limit }} 轮 · {{ verification.acceptable ? '核验依据有效，等待人工验收' : '等待当前进度的有效核验结果' }}</p>
    <p v-if="verification.goal.checks.length">核验结果有效期 {{ verification.fresh_seconds / 60 }} 分钟；修改进度后请重新核验。</p>
    <p v-if="latest">最近核验：{{ labels[latest.status] || latest.status }}<span v-if="latest.status === 'queued'">（先等待审批、待办和 Agent 回合处理完毕）</span></p>
    <ul v-if="latest"><li v-for="(item, i) in latest.observations" :key="i">{{ item.system || '核验服务' }}：{{ labels[item.status] || item.status }}；{{ item.reason || `当前权限 ${item.actual || '无'}，期望 ${item.expected || item.role}` }}</li></ul>
    <button v-if="verification.goal.checks.length && room.status === 'open' && room.can_write !== false" :disabled="busy.verify || ['queued', 'running'].includes(latest?.status ?? '')" @click="act('verify')">重新核验</button>
    <button v-if="canManage && room.status === 'open' && !draft" @click="edit">配置核验条件</button>
    <form v-if="draft" @submit.prevent="save">
      <div v-for="(check, i) in draft.checks" :key="i" class="step-editor">
        <label>系统<select v-model="check.system" @change="check.role = roles(check.system)[0]"><option v-for="system in verification.catalog" :key="system.system">{{ system.system }}</option></select></label>
        <label>最低权限<select v-model="check.role"><option v-for="role in roles(check.system)" :key="role">{{ role }}</option></select></label>
        <button type="button" @click="draft.checks.splice(i, 1)">移除条件</button>
      </div>
      <button type="button" :disabled="draft.checks.length >= 10 || !verification.catalog.length" @click="add">添加权限条件</button>
      <label>未通过时，自动只读跟进的最多轮数<select v-model.number="draft.replan_limit"><option :value="0">0（直接人工跟进）</option><option :value="1">1</option><option :value="2">2</option></select></label>
      <p v-if="!draft.checks.length">保存空条件后，该事项将使用人工验收。</p>
      <button :disabled="busy.goal">保存并核验</button><button type="button" @click="draft = null">取消</button>
    </form>
    <details v-if="checkpoints.length"><summary>已保存 {{ checkpoints.length }} 个执行检查点</summary><p>中断重试会复用同一回合中已完成的步骤。</p><ul><li v-for="point in checkpoints" :key="point.run_id + point.step_key">{{ point.step_key }} · {{ new Date(point.created_at * 1000).toLocaleString() }}</li></ul></details>
  </details>
</template>
<script setup lang="ts">
import { computed, ref } from 'vue'
import type { TaskAct } from '../lib/taskDetailsController.ts'
import type { TaskCheckpoint, TaskRoom, TaskVerification } from '../lib/types.ts'
const props = defineProps<{ verification: TaskVerification; checkpoints: TaskCheckpoint[]; room: TaskRoom; canManage: boolean; busy: Record<string, boolean>; act: TaskAct }>()
const draft = ref<TaskVerification['goal'] | null>(null)
const latest = computed(() => props.verification.history.find(v => v.id === props.verification.goal.latest_id))
const labels: Record<string, string> = { queued: '等待核验', running: '核验中', passed: '已通过', failed: '未达标', unknown: '需人工核查', stale: '已过期，请重新核验', cancelled: '已取消' }
function roles(system: string) { return props.verification.catalog.find(s => s.system === system)?.roles || [] }
function edit() { draft.value = JSON.parse(JSON.stringify(props.verification.goal)) }
function add() {
  const system = props.verification.catalog[0]?.system
  if (!draft.value || !system) return
  draft.value.checks.push({ kind: 'access_level', system, role: roles(system)[0] ?? '' })
}
async function save() {
  if (draft.value && await props.act('goal', { version: draft.value.version, checks: draft.value.checks, replan_limit: draft.value.replan_limit })) draft.value = null
}
</script>
