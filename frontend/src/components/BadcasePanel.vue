<template>
  <section class="workspace-card">
    <div class="card-heading"><h2>Badcase 标注</h2><button @click="load">刷新样本</button></div>
    <label>状态<select v-model="status" @change="load"><option value="">全部</option><option v-for="value in states" :key="value">{{ value }}</option></select></label>
    <p v-if="error" class="danger" role="alert">{{ error }}</p>
    <p v-if="!rows.length" class="muted">暂无样本。</p>
    <ul><li v-for="row in rows" :key="row.id"><button class="quiet-button" @click="selected = row">{{ row.request_id }} · {{ row.source }} · {{ row.status }}</button></li></ul>
    <article v-if="selected">
      <h3>{{ selected.request_id }}</h3><p>信号：{{ selected.signals.join('、') || '成功抽样' }}</p>
      <p v-if="selected.snapshot.snapshot_partial" class="warning">正文已过期，仅保留回合元数据，不能提升为回归用例。</p>
      <p v-if="selected.requires_second_label">此样本需要不同标注人完成第二次标注。</p>
      <details><summary>脱敏快照</summary><pre>{{ JSON.stringify(selected.snapshot, null, 2) }}</pre></details>
      <p v-if="selected.labeler">首次标注：{{ selected.labeler }} · {{ selected.label.root_cause }}</p>
      <form v-if="!['promoted', 'dismissed'].includes(selected.status)" @submit.prevent="act('label')">
        <label>根因<select v-model="rootCause"><option v-for="cause in causes" :key="cause">{{ cause }}</option></select></label>
        <label>严重级别<select v-model="severity"><option v-for="level in ['P0', 'P1', 'P2', 'P3']" :key="level">{{ level }}</option></select></label>
        <label>期望结果（JSON）<textarea v-model="expected" rows="6" required /></label>
        <small>字段：intent、tools、forbid_tools、outcome、forbid_text、evidence；授权用例另填 system 和 role。</small>
        <label>标注说明<textarea v-model="note" maxlength="2000" rows="3" /></label>
        <div><button :disabled="busy">保存标注</button><button type="button" :disabled="busy" @click="act('dismiss')">排除样本</button>
          <button v-if="selected.status === 'labeled'" type="button" :disabled="busy" @click="act('promote')">提升为回归用例</button></div>
      </form>
    </article>
  </section>
</template>
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { jsonBody, requestFor } from '../api/http.ts'
import type { Settings } from '../lib/types.ts'
const props = defineProps<{ settings: Settings }>()
const rows = ref<any[]>([]), selected = ref<any>(null), status = ref('new'), error = ref(''), busy = ref(false)
const rootCause = ref('false_claim'), severity = ref('P2'), expected = ref('{"outcome":"free","forbid_text":["已执行"]}'), note = ref('')
const states = ['new', 'triaged', 'labeled', 'promoted', 'dismissed']
const causes = ['intent_wrong', 'slot_missing_or_default', 'retrieval_miss', 'retrieval_irrelevant', 'tool_not_called', 'tool_wrong_args', 'false_claim', 'policy_explain', 'language_tone', 'latency', 'model_error', 'product_gap', 'not_a_bug']
async function load() {
  error.value = ''
  try { rows.value = (await requestFor(props.settings.backend, props.settings, '/admin/badcases?' + new URLSearchParams(status.value ? { status: status.value } : {}))).items }
  catch (cause) { error.value = String(cause) }
}
async function act(action: string) {
  if (!selected.value) return
  busy.value = true; error.value = ''
  try {
    const body = action === 'label' ? { root_cause: rootCause.value, severity: severity.value, expected: JSON.parse(expected.value), note: note.value } : {}
    selected.value = await requestFor(props.settings.backend, props.settings, `/admin/badcases/${selected.value.id}/${action}`, jsonBody(body))
    await load()
  } catch (cause) { error.value = String(cause) }
  finally { busy.value = false }
}
onMounted(load)
</script>
<style scoped>
form { display: grid; gap: 12px; margin-top: 16px; } label { display: grid; gap: 4px; }
pre { overflow: auto; max-height: 400px; white-space: pre-wrap; } button { margin-right: 8px; }
</style>
