<template>
  <details class="task-panel"><summary>参与人与审批指派</summary>
    <p>仅申请人、受邀成员、指定审批人和本租户 IT 管理员可访问。同部门身份不会自动授予访问权。</p>
    <p v-for="member in members" :key="member.subject">{{ member.profile.username }} · {{ labels[member.role] }}</p>
    <p v-if="reviewers.length">指定审批人：{{ reviewers.map(id => names[id] || id).join('、') }}</p><p v-else>未指定审批人；具备审批角色的受邀协作者可以审批。</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <template v-if="canManage">
      <button @click="load">读取已登录的租户成员</button>
      <form @submit.prevent="act('members', {subject, role})"><select v-model="subject" required><option value="" disabled>选择成员</option><option v-for="person in directory" :key="person.subject" :value="person.subject">{{ person.username }} · {{ person.department }}</option></select><select v-model="role"><option value="collaborator">协作者</option><option value="observer">观察者</option><option value="revoked">移除访问</option></select><button :disabled="busy.members || !subject">更新成员</button></form>
      <form v-if="isAdmin" @submit.prevent="act('reviewers', {subjects: selected})"><label>审批负责人（指定后获得事项访问权）<select v-model="selected" multiple><option v-for="person in directory.filter(p => p.roles.some(r => ['approver','it_admin'].includes(r)) && p.subject !== room.requester)" :key="person.subject" :value="person.subject">{{ person.username }}</option></select></label><button :disabled="busy.reviewers">保存审批指派</button></form>
    </template>
  </details>
</template>
<script setup lang="ts">
import { computed, ref, watch, onBeforeUnmount } from 'vue'
import type { TaskClient } from '../lib/taskClient.ts'
import type { TaskAct } from '../lib/taskDetailsController.ts'
import type { DirectoryPerson, TaskMember, TaskRoom } from '../lib/types.ts'
const props = defineProps<{ members: TaskMember[]; reviewers: string[]; room: TaskRoom; client: Pick<TaskClient, 'directory'>; canManage: boolean; isAdmin: boolean; busy: Record<string, boolean>; act: TaskAct }>()
const directory = ref<DirectoryPerson[]>([]), subject = ref(''), role = ref('collaborator'), selected = ref([...props.reviewers]), error = ref('')
watch(() => [...props.reviewers].sort().join('\n'), () => { selected.value = [...props.reviewers] })
const labels: Record<string, string> = { collaborator: '协作者', observer: '观察者', revoked: '已移除' }
const names = computed<Record<string, string>>(() => Object.fromEntries([...props.members.map(m => [m.subject, m.profile.username]), ...directory.value.map(p => [p.subject, p.username])]))
let alive = true
onBeforeUnmount(() => { alive = false })
async function load() {
  try { const result = await props.client.directory(props.room.id); if (alive) directory.value = result.items }
  catch (e) { if (alive) error.value = e instanceof Error ? e.message : String(e) }
}
</script>
