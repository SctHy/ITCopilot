<template>
  <div class="feedback-actions">
    <button type="button" class="quiet-button" :disabled="busy" :aria-pressed="rating === 1" @click="submit(1)">👍 有帮助</button>
    <button type="button" class="quiet-button" :disabled="busy" :aria-pressed="rating === -1" @click="editing = !editing">👎 需改进</button>
    <form v-if="editing" @submit.prevent="submit(-1)">
      <label>原因<select v-model="reason"><option v-for="(label, key) in reasons" :key="key" :value="key">{{ label }}</option></select></label>
      <label>备注（选填）<textarea v-model="comment" maxlength="500" rows="2" /></label>
      <button :disabled="busy">提交反馈</button>
    </form>
    <small v-if="saved" role="status">反馈已保存</small><small v-if="error" role="alert">{{ error }}</small>
  </div>
</template>
<script setup lang="ts">
import { ref } from 'vue'
import { jsonBody, requestFor } from '../api/http.ts'
import type { Settings } from '../lib/types.ts'
const props = defineProps<{ settings: Settings; path: string }>()
const busy = ref(false), editing = ref(false), rating = ref(0), reason = ref('wrong_answer'), comment = ref(''), error = ref(''), saved = ref(false)
const reasons = { wrong_answer: '答案有误', not_done: '事情没办完', false_claim: '声称做了但未完成', slow: '回复慢', wrong_language: '语言不合适', unsafe: '处理不安全', other: '其他' }
async function submit(value: number) {
  busy.value = true; error.value = ''; saved.value = false
  try {
    await requestFor(props.settings.backend, props.settings, props.path, jsonBody({ rating: value, reason: value === 1 ? 'other' : reason.value, comment: comment.value }))
    rating.value = value; editing.value = false; saved.value = true
  } catch (cause) { error.value = String(cause) }
  finally { busy.value = false }
}
</script>
<style scoped>
.feedback-actions { margin-top: 12px; display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
form { display: grid; flex-basis: 100%; gap: 8px; max-width: 480px; }
label { display: grid; gap: 4px; }
</style>
