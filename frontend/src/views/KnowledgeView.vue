<template>
  <section class="page page-knowledge">
    <div class="page-heading">
      <div class="heading-copy">
        <span class="kicker">KNOWLEDGE OPERATIONS</span>
        <h1>知识库</h1>
        <p>管理 IT 知识，为每次回答提供可追溯依据。</p>
      </div>
      <div class="count-display">
        <strong>{{ knowledgeCount }}</strong>
        <span>知识片段</span>
      </div>
    </div>
    <div class="knowledge-layout">
      <section class="workspace-card search-workspace">
        <div class="card-heading">
          <div>
            <span class="kicker">RETRIEVAL</span>
            <h2>检索知识</h2>
          </div>
          <AppIcon name="search" />
        </div>
        <form class="search-line" @submit.prevent="perform('search', searchKnowledge)">
          <input v-model="searchQuery" aria-label="检索问题" placeholder="例如：VPN 连不上怎么办" />
          <button :disabled="busy || !searchQuery.trim()">
            <AppIcon name="search" />
            {{ pending === 'search' ? '检索中' : '搜索' }}
          </button>
        </form>
        <div v-if="searchResults.length" class="result-list">
          <article v-for="(item, index) in searchResults" :key="item.id || index" class="result-item">
            <span class="result-number">{{ String(index + 1).padStart(2, '0') }}</span>
            <div>
              <div class="result-title">
                <strong>{{ item.title || '未命名文档' }}</strong>
                <small>相关度 {{ item.score ?? '—' }}</small>
              </div>
              <p>{{ item.content }}</p>
            </div>
          </article>
        </div>
        <div v-else class="workspace-empty">
          <AppIcon name="book" />
          <h3>{{ searched ? '暂未找到相关知识' : '搜索你的知识库' }}</h3>
          <p>{{ searched ? '尝试换一个问题，或添加更多知识文档。' : '输入员工 IT 问题，查看最相关的知识片段。' }}</p>
        </div>
      </section>
      <section class="workspace-card import-workspace">
        <div class="card-heading">
          <div>
            <span class="kicker">INGESTION</span>
            <h2>添加知识</h2>
          </div>
          <AppIcon name="document" />
        </div>
        <form @submit.prevent="perform('import', submitKnowledge)">
          <label>
            <span>文档标题</span>
            <input v-model="docTitle" required placeholder="例如：VPN 排障补充 SOP" />
          </label>
          <label>
            <span>知识内容</span>
            <textarea v-model="docContent" required rows="7" placeholder="输入 IT 规范、系统说明或排障流程"></textarea>
          </label>
          <div class="side-actions">
            <button :disabled="busy || !docTitle.trim() || !docContent.trim()">
              <AppIcon name="arrow" />
              {{ pending === 'import' ? '添加中' : '添加文档' }}
            </button>
            <label class="upload-button" :class="{ disabled: busy }">
              <AppIcon name="upload" />
              上传文件
              <input :disabled="busy" type="file" accept=".txt,.md,.json" @change="onFileChange" />
            </label>
          </div>
          <p class="form-hint">支持 TXT、Markdown、JSON，最大 10 MB。</p>
        </form>
      </section>
    </div>
    <section v-if="isAdmin" class="workspace-card skills-workspace">
      <div class="card-heading">
        <div>
          <span class="kicker">DOCUMENTS</span>
          <h2>文档管理</h2>
        </div>
        <button class="text-button" :disabled="busy" @click="loadKnowledgeDocs()">
          <AppIcon name="refresh" />
          刷新
        </button>
      </div>
      <p v-if="!knowledgeDocs.length" class="muted">知识库中还没有文档。</p>
      <div v-else class="result-list">
        <article v-for="doc in knowledgeDocs" :key="doc.document_id" class="result-item">
          <div>
            <div class="result-title">
              <strong>{{ doc.title }}</strong>
              <small>{{ doc.department === '*' ? '全员可见' : doc.department }} · {{ doc.chunks }} 个片段 · 来源 {{ doc.source }}<template v-if="doc.demo_seed"> · 演示种子</template></small>
            </div>
          </div>
          <button class="quiet-button" :disabled="busy" @click="perform('delete-doc', () => removeKnowledgeDoc(doc))">删除</button>
        </article>
      </div>
    </section>
    <section class="workspace-card skills-workspace">
      <div class="card-heading">
        <div>
          <span class="kicker">LOADED SKILLS</span>
          <h2>已加载能力</h2>
        </div>
        <button class="text-button" :disabled="busy" @click="perform('skills', reloadSkillSet)">
          <AppIcon name="refresh" />
          重新加载
        </button>
      </div>
      <div class="skill-table">
        <div v-for="skill in skillsData.skills" :key="skill.name" class="skill-item">
          <span class="skill-dot"></span>
          <strong>{{ skill.name }}</strong>
          <span>{{ skill.description || '业务规范能力' }}</span>
          <small>{{ skill.content_chars || 0 }} chars</small>
        </div>
        <p v-if="!skillsData.skills?.length" class="skills-empty">暂无已加载能力，连接服务后刷新查看。</p>
      </div>
    </section>
  </section>
</template>

<script setup lang="ts">
// Keyed by connection + token in App.vue: switching either remounts the view with fresh state.
import { computed, onMounted, ref, watch } from 'vue'
import AppIcon from '../components/AppIcon.vue'
import { addKnowledge, deleteKnowledgeDocument, listKnowledgeDocuments, requestKnowledgeStats, requestSearch, uploadKnowledge } from '../api/knowledge.ts'
import { reloadSkills, requestSkills } from '../api/system.ts'
import { loadCurrent, type RequestState } from '../lib/requestState.ts'
import type { Identity } from '../lib/types.ts'

interface SearchResult { id?: string; title?: string; content: string; score?: number }
interface KnowledgeDoc { document_id: string; title: string; department: string; chunks: number; source: string; demo_seed?: boolean }
interface Skill { name: string; description?: string; content_chars?: number }
interface SkillSet { count: number; skills: Skill[]; errors: unknown[] }

const props = defineProps<{ requestState: RequestState; identity: Identity | null }>()
const emit = defineEmits<{ toast: [message: string]; status: [text: string] }>()

const emptySkills = (): SkillSet => ({ count: 0, skills: [], errors: [] })
const knowledgeCount = ref<number | string>('-')
const searchQuery = ref('VPN 连不上怎么办')
const searchResults = ref<SearchResult[]>([])
const searched = ref(false)
const docTitle = ref('')
const docContent = ref('')
const knowledgeDocs = ref<KnowledgeDoc[]>([])
const skillsData = ref<SkillSet>(emptySkills())
const pending = ref('')
const busy = computed(() => pending.value !== '')
const isAdmin = computed(() => (props.identity?.roles || []).includes('it_admin'))

onMounted(() => { loadStats(); loadSkills(); loadKnowledgeDocs() })
watch(isAdmin, () => loadKnowledgeDocs())

// One knowledge action at a time; the slot is separate from chat so the two never invalidate each other.
async function perform(name: string, action: () => Promise<void>) {
  if (busy.value) return
  pending.value = name
  try { await action() } finally { pending.value = '' }
}

function errorMessage(error: unknown) { return error instanceof Error ? error.message : String(error) }

function loadStats() {
  return loadCurrent<{ total_chunks?: number; totalChunks?: number }>(props.requestState, 'stats', requestKnowledgeStats,
    data => { knowledgeCount.value = data.total_chunks ?? data.totalChunks ?? '-' },
    () => { knowledgeCount.value = '-' })
}

function loadSkills() {
  return loadCurrent<SkillSet>(props.requestState, 'skills', requestSkills,
    data => { skillsData.value = data },
    () => { skillsData.value = emptySkills() })
}

function loadKnowledgeDocs() {
  if (!isAdmin.value) { knowledgeDocs.value = []; return }
  return loadCurrent<{ items: KnowledgeDoc[] }>(props.requestState, 'knowledge-docs', listKnowledgeDocuments,
    body => { knowledgeDocs.value = body.items },
    error => emit('toast', `文档列表加载失败：${error.message}`))
}

async function searchKnowledge() {
  if (!searchQuery.value.trim()) return
  const request = props.requestState.begin('knowledge-action')
  try {
    const data = await requestSearch(request.backend, request.settings, searchQuery.value, 5)
    if (!request.isCurrent()) return
    searchResults.value = data.results || []
    searched.value = true
    emit('toast', `检索完成，返回 ${searchResults.value.length} 条结果`)
  } catch (error) {
    if (!request.isCurrent()) return
    emit('status', errorMessage(error))
    emit('toast', '检索失败，请检查连接')
  }
}

async function submitKnowledge() {
  if (!docTitle.value.trim() || !docContent.value.trim()) return
  const request = props.requestState.begin('knowledge-action')
  try {
    const data = await addKnowledge(request.backend, request.settings, [{ title: docTitle.value.trim(), content: docContent.value.trim() }])
    if (!request.isCurrent()) return
    emit('status', JSON.stringify(data, null, 2))
    await Promise.all([loadStats(), loadKnowledgeDocs()])
    if (request.isCurrent()) emit('toast', '文档已添加')
  } catch (error) {
    if (!request.isCurrent()) return
    emit('status', errorMessage(error))
    emit('toast', '文档导入失败')
  }
}

function onFileChange(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  if (file.size > 10 * 1024 * 1024) { emit('toast', '文件不能超过 10 MB'); return }
  perform('upload', () => uploadFile(file))
}

async function uploadFile(file: File) {
  const request = props.requestState.begin('knowledge-action')
  try {
    const data = await uploadKnowledge(request.backend, request.settings, file)
    if (!request.isCurrent()) return
    emit('status', JSON.stringify(data, null, 2))
    await Promise.all([loadStats(), loadKnowledgeDocs()])
    if (request.isCurrent()) emit('toast', `${file.name} 导入成功`)
  } catch (error) {
    if (!request.isCurrent()) return
    emit('status', errorMessage(error))
    emit('toast', '文件导入失败')
  }
}

async function removeKnowledgeDoc(doc: KnowledgeDoc) {
  const reseed = doc.demo_seed ? '\n这是演示种子文档，服务重启后会按 knowledge/ 目录重新导入。' : ''
  if (!window.confirm(`确认删除《${doc.title}》？Agent 将立即检索不到它。${reseed}`)) return
  const request = props.requestState.begin('knowledge-action')
  try {
    await deleteKnowledgeDocument(request.backend, request.settings, doc.document_id)
    if (!request.isCurrent()) return
    knowledgeDocs.value = knowledgeDocs.value.filter(item => item.document_id !== doc.document_id)
    await loadStats()
    if (request.isCurrent()) emit('toast', '文档已删除')
  } catch (error) {
    if (request.isCurrent()) emit('toast', `删除失败：${errorMessage(error)}`)
  }
}

async function reloadSkillSet() {
  const request = props.requestState.begin('knowledge-action')
  try {
    const data = await reloadSkills(request.backend, request.settings)
    if (!request.isCurrent()) return
    skillsData.value = data
    emit('toast', 'Skills 已重新加载')
  } catch (error) {
    if (!request.isCurrent()) return
    emit('status', errorMessage(error))
    emit('toast', 'Skills 加载失败')
  }
}
</script>
