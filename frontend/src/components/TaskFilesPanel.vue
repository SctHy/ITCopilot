<template>
  <details class="task-panel"><summary>附件与证据材料</summary>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-for="file in files" :key="file.id"><button @click="download(file)">{{ file.name }}</button> · {{ Math.ceil(file.size / 1024) }} KB · 可在步骤中引用对应上传消息</p>
    <form v-if="room.can_write !== false && room.status === 'open'" @submit.prevent="upload"><input ref="input" type="file" required accept=".txt,.log,.csv,.json,.png,.jpg,.jpeg,.pdf" @change="choose" /><small>单个附件最多 5 MB。下载遵循事项成员权限。</small><button :disabled="busy || !file">上传材料</button></form>
  </details>
</template>
<script setup>
import { ref, onBeforeUnmount } from 'vue'
const props = defineProps({ files: Array, room: Object, client: Object })
const emit = defineEmits(['changed']), file = ref(null), input = ref(null), error = ref(''), busy = ref(false)
let requestId = '', alive = true
onBeforeUnmount(() => { alive = false })
function choose(e) { file.value = e.target.files[0]; requestId = crypto.randomUUID() }
async function upload() {
  if (file.value.size > 5 * 1024 * 1024) { error.value = '附件上限为 5 MB'; return }
  busy.value = true; error.value = ''
  try { await props.client.upload(props.room.id, file.value, requestId); if (alive) { file.value = null; input.value.value = ''; emit('changed') } }
  catch(e) { if (alive) error.value = e.message } finally { if (alive) busy.value = false }
}
async function download(file) {
  try {
    const blob = await props.client.download(props.room.id, file.id)
    if (!alive) return
    const url = URL.createObjectURL(blob), link = document.createElement('a'); link.href = url; link.download = file.name; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch(e) { if (alive) error.value = e.message }
}
</script>
