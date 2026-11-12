<template>
  <section class="page task-workspace">
    <header class="task-heading">
      <div><h1>共享事项</h1><p>申请人、Agent 与审批人在同一事项中协作。邀请成员或指定审批人后，对方才能访问。</p></div>
      <button @click="beginNew">新建事项</button>
    </header>
    <p v-if="error" class="task-error" role="alert">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <div class="task-layout">
      <aside class="task-list" aria-label="事项列表">
        <form @submit.prevent="search({ ...query, offset: 0 })"><input v-model="query.q" maxlength="200" placeholder="搜索标题与讨论" /><button>搜索</button></form>
        <label>筛选<select v-model="query.category" @change="search({offset: 0})"><option value="all">全部事项</option><option value="unread">未读</option><option value="answer">待我补充</option><option value="approval">待我审批</option></select></label>
        <button v-for="item in items" :key="item.id" :class="{ selected: selected === item.id }" @click="select(item.id)">
          <strong>{{ item.title }}</strong><span v-if="item.unread">{{ item.unread }} 条未读</span><small>{{ item.requester_name }} · {{ item.status === 'open' ? '跟进中' : item.status === 'resolved' ? '已解决' : '已停止' }}</small>
        </button>
        <p v-if="!items.length" class="muted">暂无符合筛选条件的共享事项。</p>
        <label><input v-model="query.archived" type="checkbox" @change="search({offset: 0})" />查看我的归档</label>
        <label>状态<select v-model="query.status" @change="search({offset: 0})"><option value="">全部状态</option><option value="open">跟进中</option><option value="resolved">已解决</option><option value="stopped">已停止</option></select></label>
        <span>共 {{ total }} 个事项</span><button :disabled="query.offset === 0" @click="search({offset: Math.max(0, query.offset - query.limit)})">上一页</button><button :disabled="!hasMore" @click="search({offset: query.offset + query.limit})">下一页</button>
      </aside>
      <main class="task-room">
        <form v-if="creating" class="task-form" @submit.prevent="create">
          <h2>发起共享事项</h2>
          <label>事项名称<input v-model="title" required maxlength="200" placeholder="例如：仓库权限申请与访问排查" /></label>
          <label>需求描述<textarea v-model="initialMessage" required maxlength="8000" rows="5" placeholder="描述需要办理的事情，以及完成后还需要 Agent 跟进什么。" /></label>
          <p class="muted">创建后可在“参与人与审批指派”中邀请人员。这里的记录与私人对话分开保存。</p>
          <button :disabled="busy || !title.trim() || !initialMessage.trim()">{{ busy ? '提交中…' : '创建并请 Agent 处理' }}</button>
        </form>
        <template v-else-if="room">
          <header class="room-heading">
            <div><h2>{{ room.title }}</h2><p>申请人 {{ room.requester_name }} · {{ phases[room.phase] || room.phase }}</p></div>
            <button class="quiet-button" @click="copyLink">复制事项链接</button>
          </header>
          <p class="muted">在讨论中说“同意”不会执行操作。请在对应消息的审批卡片中选择“同意审批”或“拒绝申请”。</p>
          <button class="quiet-button" @click="archive">{{ query.archived ? '移出归档' : '归档到个人列表' }}</button>
          <button class="quiet-button" @click="markRead">标记当前消息已读</button>
          <TaskDetails ref="details" :key="room.id" :room="room" :identity="identity" :client="client" @changed="refresh" @open="select" />
          <button v-if="room.has_more_events" @click="older">加载更早的讨论</button>
          <TaskConversation :settings="settings" :room="room" :identity="identity" :approval-busy="approvalBusy" :approval-errors="approvalErrors" @approval="handleApproval" />
          <p v-if="room.phase === 'working'" role="status">Agent 正在处理，可以继续讨论或离开页面，结果会保留在这里。</p>
          <div v-for="run in failedRuns" :key="run.id" class="task-retry">
            <span>有一个 Agent 回合处理失败。</span>
            <button v-if="room.can_write !== false" :disabled="busy || room.status !== 'open'" @click="retry(run.id)">重新处理</button>
          </div>
          <form v-if="room.status === 'open' && room.can_write !== false" class="task-form" @submit.prevent="send">
            <label>参与讨论<textarea ref="composer" v-model="message" required maxlength="8000" rows="3" placeholder="补充需求、询问依据，或请 Agent 继续排查…" /></label>
            <label class="task-checkbox"><input v-model="askAgent" type="checkbox" />同时请 Agent 回复</label>
            <div class="task-actions"><button :disabled="busy || !message.trim()">发送</button>
              <button v-if="canStop" type="button" class="quiet-button" :disabled="busy" @click="stop">停止 Agent 跟进</button></div>
            <small class="muted">停止跟进仅停止 Agent 回合，现有审批与已批准的业务动作继续按原工作流处理。</small>
          </form>
        </template>
        <p v-else class="muted">选择一个事项，查看共同讨论与处理结果。</p>
      </main>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, toRefs, watch } from 'vue'
import TaskConversation from './TaskConversation.vue'
import TaskDetails from './TaskDetails.vue'
import { createTaskClient } from '../lib/taskClient.ts'
import { createTaskController } from '../lib/taskController.ts'
import { createPolling } from '../lib/stateMachine.ts'
import type { Approval, ApprovalDecision, Identity, Settings } from '../lib/types.ts'

const props = defineProps<{ settings: Settings; identity: Identity | null }>()
// App remounts this workspace when identity or endpoint changes.
const client = createTaskClient(props.settings)
const controller = createTaskController(client)
const details = ref<InstanceType<typeof TaskDetails> | null>(null)

const { items, room, selected, creating, title, initialMessage, message, askAgent, busy, error, notice, approvalBusy, approvalErrors, query, total, hasMore } = toRefs(controller.state)
const { refresh, search, older, select, beginNew, create, send, retry, stop } = controller
const composer = ref<HTMLTextAreaElement | null>(null)
const phases: Record<string, string> = { working: 'Agent 处理中', waiting: '等待后续消息', awaiting_confirmation: '等待申请人确认提交', awaiting_approval: '等待审批', executing: '业务执行中', manual_review: '需人工核对', failed: 'Agent 回合失败', stopped: '已停止跟进', resolved: '已解决', budget_paused: '累计预算已用完，等待管理员调整' }
const failedRuns = computed(() => (room.value?.runs || []).filter(run => run.status === 'failed'))
const canStop = computed(() => room.value?.requester === props.identity?.subject || !!props.identity?.roles?.includes('it_admin'))
const polling = createPolling(refresh, 2000)
const message_ = (e: unknown) => e instanceof Error ? e.message : String(e)
async function handleApproval(approval: Approval, decision?: ApprovalDecision) {
  if (decision === 'clarify') { details.value?.clarify(approval); return }
  await controller.decide(approval, decision)
}
async function archive() { try { await client.capability(selected.value, 'archive', { archived: !query.value.archived }); await refresh() } catch (e) { error.value = message_(e) } }
async function markRead() {
  try { await client.capability(selected.value, 'read', { seq: Math.max(0, ...(room.value?.events ?? []).map(e => e.seq)) }); await refresh() }
  catch (e) { error.value = message_(e) }
}
async function copyLink() {
  const url = new URL(window.location.href)
  url.search = ''; url.hash = ''; url.searchParams.set('task', selected.value)
  try { await navigator.clipboard.writeText(url.href); notice.value = '事项链接已复制；访问者仍需登录并具备相应权限。' }
  catch { notice.value = url.href }
}
onMounted(() => {
  selected.value = new URL(window.location.href).searchParams.get('task') || ''
  refresh(); polling.start()
  // Keep ?task= in step with the open room, so reload or coming back reopens what the user last chose.
  watch(selected, id => {
    const url = new URL(window.location.href)
    if (id) url.searchParams.set('task', id); else url.searchParams.delete('task')
    window.history.replaceState({}, '', url)
  })
})
onBeforeUnmount(() => { controller.dispose(); polling.stop() })
</script>

<style scoped>
.task-workspace { max-width: 1280px; width: 100%; }
.task-heading, .room-heading, .task-actions, .task-retry { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.task-heading { margin-bottom: 24px; }
.task-heading p, .room-heading p { color: var(--muted); line-height: 1.7; }
.task-layout { display: grid; grid-template-columns: minmax(180px, 250px) minmax(0, 1fr); gap: 28px; }
.task-list { display: flex; flex-direction: column; gap: 10px; }
.task-list button { text-align: left; display: grid; gap: 8px; white-space: normal; background: transparent; color: inherit; border: 1px solid var(--line); padding: 14px; }
.task-list button.selected { border-color: var(--ink); background: var(--soft); }
.task-list button:hover { background: var(--hover); }
.task-list small { opacity: .65; font-size: 12px; }
.task-list input[type="checkbox"] { width: auto; }
.task-room { min-width: 0; }
.task-form { display: grid; gap: 14px; margin-top: 24px; }
.task-form label { display: grid; gap: 8px; }
.task-form input, .task-form textarea { width: 100%; }
.task-form .task-checkbox { display: flex; align-items: center; }
.task-checkbox input { width: auto; }
.task-error { color: var(--danger); }
@media (max-width: 760px) {
  .task-layout { grid-template-columns: minmax(0, 1fr); }
  .task-list { max-height: 200px; overflow-y: auto; }
  .task-heading, .room-heading { align-items: flex-start; flex-direction: column; }
}
</style>
