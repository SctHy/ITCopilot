<template>
  <details class="task-panel"><summary>附件与证据材料</summary>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-for="file in files" :key="file.id"><button @click="download(file)">{{ file.name }}</button> · {{ Math.ceil(file.size / 1024) }} KB · 可在步骤中引用对应上传消息</p>
    <form v-if="room.can_write !== false && room.status === 'open'" @submit.prevent="upload"><input ref="input" type="file" required accept=".txt,.log,.csv,.json,.png,.jpg,.jpeg,.pdf" @change="choose" /><small>单个附件最多 5 MB。下载遵循事项成员权限。</small><button :disabled="busy || !file">上传材料</button></form>
  </details>
</template>
<script setup lang="ts">
import { ref, onBeforeUnmount } from 'vue'
import type { TaskClient } from '../lib/taskClient.ts'
import type { TaskFile, TaskRoom } from '../lib/types.ts'
const props = defineProps<{ files: TaskFile[]; room: TaskRoom; client: Pick<TaskClient, 'upload' | 'download'> }>()
const emit = defineEmits<{ changed: [] }>()
const file = ref<File | null>(null), input = ref<HTMLInputElement | null>(null), error = ref(''), busy = ref(false)
let requestId = '', alive = true
onBeforeUnmount(() => { alive = false })
// A new file gets a new request key; re-uploading the same selection after a lost response reuses it.
function choose(e: Event) { file.value = (e.target as HTMLInputElement).files?.[0] ?? null; requestId = crypto.randomUUID() }
async function upload() {
  const chosen = file.value
  if (!chosen) return
  if (chosen.size > 5 * 1024 * 1024) { error.value = '附件上限为 5 MB'; return }
  busy.value = true; error.value = ''
  try { await props.client.upload(props.room.id, chosen, requestId); if (alive) { file.value = null; if (input.value) input.value.value = ''; emit('changed') } }
  catch (e) { if (alive) error.value = e instanceof Error ? e.message : String(e) } finally { if (alive) busy.value = false }
}
async function download(item: TaskFile) {
  try {
    const blob = await props.client.download(props.room.id, item.id)
    if (!alive) return
    const url = URL.createObjectURL(blob), link = document.createElement('a'); link.href = url; link.download = item.name; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch (e) { if (alive) error.value = e instanceof Error ? e.message : String(e) }
}
</script>
