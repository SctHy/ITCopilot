<template>
  <div class="app-shell" :class="`view-${activeView}`">
    <aside class="navigation-rail" aria-label="主导航">
      <a class="brand" href="#" @click.prevent="activeView = 'chat'" aria-label="IT-Copilot 首页">
        <span class="brand-mark"><AppIcon name="it" /></span>
        <span class="brand-name">IT-Copilot</span>
      </a>
      <button class="new-chat-button" @click="activeView = 'chat'; clearConversation()">
        <AppIcon name="plus" />
        <span>新对话</span>
      </button>
      <span class="nav-caption">工作空间</span>
      <nav class="view-nav">
        <button
          v-for="view in views"
          :key="view.id"
          :aria-label="view.label"
          :class="{ active: activeView === view.id }"
          :aria-current="activeView === view.id ? 'page' : undefined"
          @click="activeView = view.id"
        >
          <AppIcon :name="view.icon" />
          <span>{{ view.label }}</span>
          <AppIcon class="nav-chevron" name="chevron" />
        </button>
      </nav>
      <div class="rail-bottom">
        <a class="docs-link" aria-label="API 文档" :href="docsUrl" target="_blank" rel="noreferrer">
          <AppIcon name="document" />
          <span>API 文档</span>
          <AppIcon name="northeast" />
        </a>
        <button class="user-button" aria-label="当前用户与会话配置" @click="openConnection">
          <span class="avatar">{{ userInitial }}</span>
          <span>
            <strong>{{ identity?.username || '未登录' }}</strong>
            <small>{{ identity ? `${identity.department} · ${identity.roles.map(r => roleLabels[r] || r).join('、')}` : '点击登录' }}</small>
          </span>
          <AppIcon name="sliders" />
        </button>
      </div>
    </aside>
    <main class="workspace">
      <header class="topbar">
        <div class="page-breadcrumb">
          <span>工作空间</span>
          <AppIcon name="chevron" />
          <strong>{{ currentView.label }}</strong>
        </div>
        <a class="mobile-brand" href="#" @click.prevent="activeView = 'chat'" aria-label="IT-Copilot 首页">
          <span class="brand-mark"><AppIcon name="it" /></span>
          <span>IT-Copilot</span>
        </a>
        <div class="topbar-tools">
          <span class="connection-status" :title="statusText || healthLabel">
            <i :class="healthOk ? 'online' : 'offline'"></i>
            {{ healthOk ? '已连接' : healthLabel === '未检查' ? '连接中' : '未连接' }}
          </span>
          <button class="icon-button settings-button" aria-label="连接配置" title="连接配置" @click="openConnection">
            <AppIcon name="sliders" />
          </button>
        </div>
      </header>
      <div v-if="toast" class="toast" role="status">
        <AppIcon name="chat" />
        {{ toast }}
      </div>
      <section v-if="activeView === 'chat'" class="page-chat">
        <div class="chat-toolbar">
          <span class="conversation-label">
            {{ messages.length ? '当前对话' : '新对话' }}
            <span v-if="messages.length" class="conversation-count">{{ messages.length }} 条消息</span>
          </span>
          <div class="chat-toolbar-actions">
            <button class="icon-button" @click="clearConversation" aria-label="新对话" title="新对话">
              <AppIcon name="compose" />
            </button>
            <button class="trace-toggle" @click="inspectorDialog.showModal()">
              <AppIcon name="panel" />
              <span>运行详情</span>
            </button>
          </div>
        </div>
        <div class="conversation-layout" :class="{ 'has-conversation': messages.length }">
          <section class="conversation" :class="{ 'is-empty': !messages.length }" aria-label="IT 服务台对话">
            <div class="messages" ref="messageList" role="log" aria-label="IT 服务台对话" aria-live="polite" :aria-busy="busy && pendingAction === 'chat'">
              <div v-if="!messages.length" class="welcome">
                <div class="welcome-mark" aria-hidden="true"><AppIcon name="it" /></div>
                <h1>今天，需要解决什么？</h1>
                <p>从员工问题开始，让合适的 Agent 接手。</p>
              </div>
              <template v-else>
                <article v-for="item in messages" :key="item.id" :class="['message', item.role, { 'message-error': item.error }]">
                  <span v-if="item.role === 'assistant'" class="message-avatar"><AppIcon name="it" /></span>
                  <div class="message-content">
                    <div class="message-meta">
                      <strong>{{ item.role === 'user' ? '你' : 'IT-Copilot' }}</strong>
                      <span v-if="item.error" class="error-label">请求失败</span>
                      <small v-else-if="item.role === 'assistant'">{{ currentBackend.label }} Agent</small>
                    </div>
                    <span v-if="item.meta && !item.error" class="agent-tag">
                      <AppIcon name="route" />
                      {{ item.meta }}
                    </span>
                    <p>{{ formatBody(item.content) }}</p>
                    <ApprovalCard
                      v-if="item.approvalId"
                      :approval="item.approval"
                      :busy="item.approvalBusy"
                      :error="item.approvalError"
                      :identity="identity"
                      @action="handleApproval(item, $event)"
                    />
                    <details v-if="item.trace?.toolCalls?.length || item.trace?.toolsUsed?.length" class="message-trace">
                      <summary>
                        <AppIcon name="chevron" />
                        <span>工具调用</span>
                        <span class="count-tag">{{ item.trace.toolCalls?.length || item.trace.toolsUsed?.length || 0 }}</span>
                      </summary>
                      <div v-for="(call, index) in item.trace.toolCalls" :key="`${item.id}-${index}`" class="trace-call-item">
                        <div class="trace-call-meta">
                          <strong>{{ call.tool_name || 'unknown_tool' }}</strong>
                          <span :class="call.success ? 'success' : 'danger'">{{ call.success ? '成功' : '失败' }}</span>
                        </div>
                        <p v-if="call.agent_type" class="trace-call-agent">{{ call.agent_type }}</p>
                        <pre>{{ formatJson(call.input || {}) }}</pre>
                        <p v-if="!call.success && call.error" class="trace-call-error">{{ call.error }}</p>
                      </div>
                      <p v-if="!item.trace.toolCalls?.length" class="muted">已调用：{{ item.trace.toolsUsed.join(' · ') }}</p>
                    </details>
                    <button
                      v-if="item.role === 'assistant' && !item.error"
                      class="icon-button copy-button"
                      @click="copyMessage(item.content)"
                      aria-label="复制回复"
                      title="复制回复"
                    >
                      <AppIcon name="copy" />
                    </button>
                  </div>
                </article>
                <div v-if="busy && pendingAction === 'chat'" class="thinking" role="status">
                  <span class="message-avatar"><AppIcon name="it" /></span>
                  <span class="thinking-dots">
                    <i></i>
                    <i></i>
                    <i></i>
                  </span>
                  <span>{{ streamStatus || '正在处理问题' }}</span>
                </div>
              </template>
            </div>
            <div class="composer-area">
              <form class="composer" @submit.prevent="performAction('chat', sendMessage)">
                <textarea
                  ref="composerInput"
                  v-model="draft"
                  rows="2"
                  aria-label="员工 IT 问题"
                  placeholder="输入你的问题…"
                  @keydown.meta.enter.prevent="performAction('chat', sendMessage)"
                  @keydown.ctrl.enter.prevent="performAction('chat', sendMessage)"
                >
                </textarea>
                <div class="composer-bottom">
                  <span class="routing-label">
                    <AppIcon name="route" />
                    自动路由
                  </span>
                  <div class="send-controls">
                    <span class="keyboard-hint">⌘ / Ctrl + Enter</span>
                    <button
                      class="send-button"
                      type="submit"
                      :disabled="busy || !draft.trim()"
                      :aria-label="busy && pendingAction === 'chat' ? '处理中' : '发送'"
                      :title="busy && pendingAction === 'chat' ? '处理中' : '发送'"
                    >
                      <AppIcon :name="busy && pendingAction === 'chat' ? 'refresh' : 'arrow'" :class="{ spinning: busy && pendingAction === 'chat' }" />
                    </button>
                  </div>
                </div>
              </form>
              <div v-if="!messages.length" class="prompt-grid">
                <button v-for="prompt in prompts" :key="prompt.label" @click="usePrompt(prompt.text)">
                  <AppIcon :name="prompt.label === 'VPN 连不上' ? 'route' : prompt.label === '重置密码' ? 'sliders' : 'document'" />
                  <span>
                    <strong>{{ prompt.label }}</strong>
                    <small>{{ prompt.label === 'VPN 连不上' ? '检查网络、DNS 和客户端状态' : prompt.label === '重置密码' ? '走身份平台安全重置流程' : '提交最小权限申请' }}</small>
                  </span>
                  <AppIcon class="prompt-arrow" name="northeast" />
                </button>
              </div>
              <p class="composer-note">AI 生成的内容可能不准确，请结合业务规则核实。</p>
            </div>
          </section>
          <aside v-if="messages.length" class="inspector-desktop" aria-label="请求追踪与运行状态">
            <div class="inspector-heading">
              <AppIcon name="panel" />
              <h2>运行详情</h2>
            </div>
            <RequestInspector :last-response="lastResponse" :last-trace="lastTrace" :busy="busy && pendingAction === 'chat'" :monitor-data="monitorData" @refresh="loadMonitor" />
          </aside>
        </div>
      </section>
      <section v-else-if="activeView === 'knowledge'" class="page page-knowledge">
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
            <form class="search-line" @submit.prevent="performAction('search', searchKnowledge)">
              <input v-model="searchQuery" aria-label="检索问题" placeholder="例如：VPN 连不上怎么办" />
              <button :disabled="busy || !searchQuery.trim()">
                <AppIcon name="search" />
                {{ busy && pendingAction === 'search' ? '检索中' : '搜索' }}
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
            <form @submit.prevent="performAction('import', submitKnowledge)">
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
                  {{ busy && pendingAction === 'import' ? '添加中' : '添加文档' }}
                </button>
                <label class="upload-button" :class="{ disabled: busy }">
                  <AppIcon name="upload" />
                  上传文件
                  <input :disabled="busy" type="file" accept=".txt,.md,.json" @change="performAction('upload', handleUpload, $event)" />
                </label>
              </div>
              <p class="form-hint">支持 TXT、Markdown、JSON，最大 10 MB。</p>
            </form>
          </section>
        </div>
        <section class="workspace-card skills-workspace">
          <div class="card-heading">
            <div>
              <span class="kicker">LOADED SKILLS</span>
              <h2>已加载能力</h2>
            </div>
            <button class="text-button" :disabled="busy" @click="performAction('skills', reloadSkillSet)">
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
      <section v-else-if="activeView === 'approvals'" class="page page-approvals">
        <div class="page-heading">
          <div class="heading-copy">
            <span class="kicker">APPROVALS</span>
            <h1>审批</h1>
            <p>审批人看到全部待审批申请，员工只看到本人的申请。批准后由执行器写入目标系统，到期自动回收。</p>
          </div>
          <button @click="performAction('approvals', loadApprovals)" :disabled="busy">
            <AppIcon name="refresh" :class="{ spinning: busy && pendingAction === 'approvals' }" />
            刷新
          </button>
        </div>
        <div class="approval-filters">
          <button v-for="option in approvalFilters" :key="option.id" :class="{ active: approvalFilter === option.id }" @click="approvalFilter = option.id; loadApprovals()">{{ option.label }}</button>
        </div>
        <p v-if="!approvalInbox.length" class="muted">没有{{ approvalFilters.find(o => o.id === approvalFilter)?.label }}的申请。</p>
        <ApprovalCard
          v-for="item in approvalInbox"
          :key="item.approvalId"
          :approval="item.approval"
          :busy="item.approvalBusy"
          :error="item.approvalError"
          :identity="identity"
          @action="handleApproval(item, $event)"
        />
      </section>
      <section v-else class="page page-evaluation">
        <div class="page-heading">
          <div class="heading-copy">
            <span class="kicker">OPERATIONS</span>
            <h1>运行与评测</h1>
            <p>实时 SLO 燃烧率、告警与 Agent 健康度，以及最新离线评测结果。</p>
          </div>
          <button @click="performAction('operations', loadOperations)" :disabled="busy">
            <AppIcon name="refresh" :class="{ spinning: busy && pendingAction === 'operations' }" />
            刷新
          </button>
        </div>
        <div class="evaluation-content">
          <div class="evaluation-summary">
            <div class="score-hero">
              <span>SERVICE STATUS · 服务状态</span>
              <strong :class="serviceStatus.className">{{ serviceStatus.label }}</strong>
              <small>{{ monitorSummary.last_tick ? `最近评估 ${formatTime(monitorSummary.last_tick)}` : '等待监控首次评估' }}</small>
            </div>
            <div v-for="slo in monitorSummary.slos" :key="slo.name">
              <span>{{ sloLabels[slo.name] || slo.name }} · 1h 燃烧率</span>
              <strong :class="slo.burn_rate['1h'] > 6 ? 'danger' : 'success'">{{ slo.burn_rate['1h'] }}x</strong>
              <small>目标 {{ formatPercent(slo.target) }} · 近 1h {{ slo.requests_1h }} 次 / {{ slo.bad_1h }} 次不达标</small>
            </div>
            <div>
              <span>触发中的告警</span>
              <strong :class="monitorSummary.alerts.length ? 'danger' : 'success'">{{ monitorSummary.alerts.length }}</strong>
              <small>近期已恢复 {{ monitorSummary.recent_resolved.length }} 条</small>
            </div>
          </div>
          <div class="evaluation-layout">
            <section class="workspace-card">
              <div class="card-heading">
                <div>
                  <span class="kicker">ALERTS</span>
                  <h2>告警与处置建议</h2>
                </div>
              </div>
              <p v-if="!monitorSummary.alerts.length" class="muted">当前没有触发中的告警。</p>
              <div v-for="alert in monitorSummary.alerts" :key="alert.key" class="evaluation-warning">
                <strong :class="alert.severity === 'critical' ? 'danger' : 'warning'">{{ alert.severity === 'critical' ? '严重' : '警告' }} · {{ alert.key }}</strong>
                <p>{{ alert.message }}</p>
                <small v-if="alert.action">{{ alert.action }}</small>
              </div>
            </section>
            <section class="workspace-card">
              <div class="card-heading">
                <div>
                  <span class="kicker">AGENTS · TOOLS</span>
                  <h2>组件健康度</h2>
                </div>
              </div>
              <div class="score-list">
                <div v-for="(agent, key) in monitorData.agents" :key="key">
                  <span>{{ key }} · {{ agentStateLabels[agent.state] || agent.state }}</span>
                  <i>
                    <b :style="{ width: `${agent.recent_total ? agent.recent_success_rate * 100 : 0}%` }"></b>
                  </i>
                  <strong>{{ agent.recent_total ? formatPercent(agent.recent_success_rate) : '—' }}</strong>
                </div>
              </div>
              <div class="health-rows">
                <p v-for="(tool, name) in monitorData.tools" :key="name">
                  <span>{{ name }}</span>
                  <strong :class="tool.circuit_state === 'closed' ? 'success' : 'danger'">{{ tool.circuit_state }}</strong>
                </p>
                <p v-for="server in monitorData.mcp?.servers || []" :key="server.name">
                  <span>MCP · {{ server.name }}（{{ server.tools.length }} 个工具）</span>
                  <strong :class="server.state === 'connected' ? 'success' : 'danger'">{{ server.state }}</strong>
                </p>
                <p v-if="!monitorData.mcp?.servers?.length" class="muted">未启用外部 MCP 服务。</p>
              </div>
            </section>
          </div>
          <section class="workspace-card">
            <div class="card-heading">
              <div>
                <span class="kicker">OFFLINE EVALUATION</span>
                <h2>离线评测</h2>
              </div>
              <span class="count-tag" :class="evalReports?.gate.all_passed ? 'success' : 'danger'">{{ evalReports ? (evalReports.gate.all_passed ? '门禁通过' : '门禁未通过') : '未加载' }}</span>
            </div>
            <p class="muted">由 <code>scripts/evaluate_all.py</code> 基于固定数据集生成，页面只读取报告，不在线调用模型。</p>
            <div class="report-grid">
              <div v-for="suite in evalReports?.suites || []" :key="suite.id" class="report-card">
                <strong>{{ suite.title }} <small :class="suite.status === 'passed' ? 'success' : 'danger'">{{ suite.status }}</small></strong>
                <p v-for="metric in suite.metrics" :key="metric.label">
                  <span>{{ metric.label }}</span>
                  <b>{{ formatMetric(metric) }}</b>
                </p>
              </div>
            </div>
          </section>
        </div>
      </section>
    </main>
    <dialog ref="inspectorDialog" class="inspector-dialog" @click="closeOnBackdrop" aria-labelledby="inspector-title">
      <div class="drawer-heading">
        <div>
          <AppIcon name="panel" />
          <h2 id="inspector-title">运行详情</h2>
        </div>
        <button class="icon-button" aria-label="关闭运行详情" @click="inspectorDialog.close()">
          <AppIcon name="close" />
        </button>
      </div>
      <RequestInspector :last-response="lastResponse" :last-trace="lastTrace" :busy="busy && pendingAction === 'chat'" :monitor-data="monitorData" @refresh="loadMonitor" />
    </dialog>
    <dialog ref="connectionDialog" class="connection-dialog" @click="closeOnBackdrop" aria-labelledby="connection-title">
      <div class="dialog-heading">
        <div>
          <span class="kicker">CONNECTION</span>
          <h2 id="connection-title">连接配置</h2>
        </div>
        <button class="icon-button" aria-label="关闭连接配置" @click="connectionDialog.close()">
          <AppIcon name="close" />
        </button>
      </div>
      <p class="dialog-description">配置 {{ currentBackend.label }} 服务与当前会话。</p>
      <form @submit.prevent="saveConnection">
        <label>
          <span>服务地址</span>
          <input v-model="connectionDraft.endpoint" required placeholder="http://127.0.0.1:8001" />
          <small>使用代理路径或完整的 HTTP / HTTPS 地址。</small>
        </label>
        <div class="identity-block">
          <span>当前身份</span>
          <strong>{{ identity ? `${identity.username} · 工号 ${identity.employee_id || '-'} · ${identity.department}` : '未登录' }}</strong>
          <small v-if="identity">角色：{{ identity.roles.map(r => roleLabels[r] || r).join('、') || '无' }}（来自已验证令牌，服务端强制执行）</small>
          <template v-if="authConfig?.mode === 'dev'">
            <select v-model="devUser" aria-label="演示身份">
              <option v-for="user in authConfig.users" :key="user.username" :value="user.username">
                {{ user.username }} · {{ user.department }} · {{ user.roles.map(r => roleLabels[r] || r).join('、') }}
              </option>
            </select>
            <button type="button" class="quiet-button" @click="loginAs(devUser)">切换到该演示身份</button>
          </template>
          <template v-else-if="authConfig?.mode === 'oidc'">
            <button v-if="!identity" type="button" @click="loginOidc">通过 Keycloak 登录</button>
            <button v-else type="button" class="quiet-button" @click="logoutOidc">退出登录</button>
          </template>
        </div>
        <label>
          <span>会话 ID</span>
          <input v-model="connectionDraft.conversationId" placeholder="留空以自动生成" />
        </label>
        <div class="dialog-health">
          <i :class="healthOk ? 'online' : 'offline'"></i>
          <span>{{ healthOk ? '服务已连接' : healthLabel }}</span>
          <button type="button" class="text-button" @click="checkHealth">
            检查当前连接
            <AppIcon name="refresh" />
          </button>
        </div>
        <details v-if="statusText" class="connection-detail">
          <summary>查看连接详情</summary>
          <pre>{{ statusText }}</pre>
        </details>
        <div class="dialog-actions">
          <button type="button" class="quiet-button" @click="connectionDialog.close()">取消</button>
          <button type="submit">
            保存配置
            <AppIcon name="check" />
          </button>
        </div>
      </form>
    </dialog>
  </div>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import {
  addKnowledge,
  backendMeta,
  createInitialSettings,
  reloadSkills,
  requestChat,
  requestChatStream,
  requestHealth,
  requestApproval,
  reconcileApproval,
  listApprovals,
  requestAuthConfig,
  requestDevToken,
  requestMe,
  requestKnowledgeStats,
  requestMonitor,
  requestSearch,
  requestToolTrace,
  requestSkills,
  requestEvaluationReports,
  saveSettings,
  uploadKnowledge
} from './lib/backends'

import AppIcon from './components/AppIcon.vue'
import ApprovalCard from './components/ApprovalCard.vue'
import { completeOidcLogin, oidcLogout, startOidcLogin, storeToken } from './lib/auth.js'
import RequestInspector from './components/RequestInspector.vue'

import { createRequestState } from './lib/requestState'

const settings = reactive(createInitialSettings())
const requestState = createRequestState(settings)
const activeView = ref('chat')
const messages = ref([])
const draft = ref('')
const busy = ref(false)
const healthOk = ref(false)
const healthLabel = ref('未检查')
const statusText = ref('')
const knowledgeCount = ref('-')
const searchQuery = ref('VPN 连不上怎么办')
const searchResults = ref([])
const docTitle = ref('')
const docContent = ref('')
const messageList = ref(null)
const composerInput = ref(null)
const connectionDialog = ref(null)
const inspectorDialog = ref(null)
const connectionDraft = reactive({ endpoint: '', conversationId: '' })
const identity = ref(null)
const authConfig = ref(null)
const devUser = ref('alice')
const approvalInbox = ref([])
const approvalFilter = ref('pending')
const approvalFilters = [{ id: 'pending', label: '待审批' }, { id: 'approved', label: '已批准' }, { id: 'rejected', label: '已拒绝' }]
const roleLabels = { employee: '员工', approver: '审批人', it_admin: 'IT 管理员', auditor: '审计员' }
const pendingAction = ref('')
const streamStatus = ref('')
const searched = ref(false)
const views = [
  { id: 'chat', label: '对话', icon: 'chat' },
  { id: 'knowledge', label: '知识库', icon: 'book' },
  { id: 'approvals', label: '审批', icon: 'shield' },
  { id: 'evaluation', label: '运行与评测', icon: 'chart' }
]
const prompts = [
  { label: 'VPN 连不上', text: 'VPN 连不上，错误码 VPN-691，应该怎么排查？' },
  { label: '重置密码', text: '我忘记了企业密码，如何安全重置？' },
  { label: '申请权限', text: '请帮我申请 GitLab 项目只读权限。' }
]
const currentView = computed(() => views.find(view => view.id === activeView.value))
const emptyMonitor = () => ({ agents: {}, tools: {}, monitor: null, mcp: { servers: [] } })
const monitorData = ref(emptyMonitor())
const skillsData = ref({ count: 0, skills: [], errors: [] })
const lastResponse = ref(null)
const lastTrace = ref(null)
const evalReports = ref(null)
const sloLabels = { chat_availability: '可用性', chat_latency: '延迟达标率' }
const agentStateLabels = { healthy: '健康', cooling: '降级冷却', half_open: '待探测', probing: '探测中' }
const monitorSummary = computed(() => monitorData.value.monitor || { status: 'unknown', slos: [], alerts: [], recent_resolved: [], last_tick: null })
const serviceStatus = computed(() => ({
  ok: { label: '正常', className: 'success' },
  degraded: { label: '降级', className: 'warning' },
  critical: { label: '严重', className: 'danger' }
}[monitorSummary.value.status] || { label: monitorData.value.loaded ? '未知' : '未连接', className: '' }))
let operationsTimer
const toast = ref('')
let toastTimer
let messageSequence = 0

const currentBackend = computed(() => backendMeta(settings.backend, settings))
const docsUrl = computed(() => `${currentBackend.value.baseUrl}/docs`)
const userInitial = computed(() => (identity.value?.username || '?').slice(0, 1).toUpperCase())

watch(() => settings.conversationId, persist)
watch(activeView, view => {
  clearInterval(operationsTimer)
  if (view === 'approvals') loadApprovals()
  if (view !== 'evaluation') return
  loadOperations()
  operationsTimer = setInterval(loadOperations, 15000)
})
onMounted(async () => {
  await initAuth()
  refreshConsole()
})
onBeforeUnmount(() => {
  requestState.invalidate()
  clearTimeout(toastTimer)
  clearInterval(operationsTimer)
})

function persist() { saveSettings(settings) }

function performAction(name, action, ...args) {
  if (busy.value) return
  pendingAction.value = name
  return action(...args)
}

// 身份初始化：先处理 OIDC 回调；dev 模式无令牌时默认以 alice 登录，便于本地演示。
async function initAuth() {
  try {
    const callbackToken = await completeOidcLogin()
    if (callbackToken) settings.accessToken = callbackToken
    authConfig.value = await requestAuthConfig(settings.backend, settings)
    if (!settings.accessToken && authConfig.value.mode === 'dev') await loginAs(identity.value?.username || 'alice', { quiet: true })
    await loadIdentity()
  } catch (error) {
    showToast(`身份初始化失败：${error.message}`)
  }
}

async function loadIdentity() {
  if (!settings.accessToken) { identity.value = null; return }
  try {
    identity.value = await requestMe(settings.backend, settings)
    devUser.value = identity.value.username
  } catch {
    identity.value = null
    settings.accessToken = ''
    storeToken('')
  }
}

async function loginAs(username, { quiet = false } = {}) {
  const { access_token: token } = await requestDevToken(settings.backend, settings, username)
  settings.accessToken = token
  storeToken(token)
  // 换身份即换会话：避免把上一个人的对话与审批卡片展示给新身份。
  clearConversation()
  approvalInbox.value = []
  await loadIdentity()
  if (!quiet) showToast(`已切换为 ${username}`)
}

function loginOidc() { startOidcLogin(authConfig.value) }
function logoutOidc() { oidcLogout(authConfig.value) }

async function loadApprovals() {
  if (!settings.accessToken) return
  try {
    const body = await listApprovals(settings.backend, settings, approvalFilter.value)
    approvalInbox.value = body.items.map(approval => ({ approvalId: approval.approval_id, approval, approvalBackend: settings.backend, approvalSettings: { ...settings } }))
  } catch (error) {
    showToast(`审批列表加载失败：${error.message}`)
  }
}

function openConnection() {
  connectionDraft.endpoint = currentBackend.value.baseUrl
  connectionDraft.conversationId = settings.conversationId
  connectionDialog.value.showModal()
}

function closeOnBackdrop(event) {
  const dialog = event.currentTarget
  if (event.target !== dialog) return
  const rect = dialog.getBoundingClientRect()
  if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close()
}

function saveConnection() {
  const endpoint = connectionDraft.endpoint.trim().replace(/\/+$/, '')
  if (!endpoint || !(endpoint.startsWith('/') || /^https?:\/\//i.test(endpoint))) {
    showToast('请输入代理路径或 HTTP / HTTPS 服务地址')
    return
  }
  const changed = endpoint !== currentBackend.value.baseUrl || connectionDraft.conversationId.trim() !== settings.conversationId
  if (changed) {
    requestState.invalidate()
    busy.value = false
    messages.value = []
    lastResponse.value = null
    lastTrace.value = null
    searchResults.value = []
    searched.value = false
    evalReports.value = null
    monitorData.value = emptyMonitor()
    skillsData.value = { count: 0, skills: [], errors: [] }
    knowledgeCount.value = '—'
    healthOk.value = false
    healthLabel.value = '未检查'
    statusText.value = ''
  }
  const identityChanged = endpoint !== currentBackend.value.baseUrl
  settings.endpoints[settings.backend] = endpoint
  settings.conversationId = identityChanged && connectionDraft.conversationId.trim() === settings.conversationId ? '' : connectionDraft.conversationId.trim()
  persist()
  connectionDialog.value.close()
  refreshConsole()
  showToast('连接配置已保存')
}

async function copyMessage(content) {
  try {
    await navigator.clipboard.writeText(content)
    showToast('回复已复制')
  } catch {
    showToast('复制失败，请手动选择回复文字')
  }
}


async function refreshConsole() {
  // /monitor 需要登录；未登录时只检查公开接口，避免无意义的 401。
  await Promise.allSettled([checkHealth(), loadStats(), settings.accessToken ? loadMonitor() : null, loadSkills()])
}

async function checkHealth() {
  const request = requestState.begin('health')

  try {
    const data = await requestHealth(request.backend, request.settings)
    if (!request.isCurrent()) return
    healthOk.value = data.status === 'ok'
    healthLabel.value = data.status || 'ok'
    statusText.value = JSON.stringify(data, null, 2)
  } catch (error) {
    if (!request.isCurrent()) return
    // 健康检查只更新状态，保留用户选择的后端。
    healthOk.value = false
    healthLabel.value = '不可用'
    statusText.value = error.message
  }
}

async function loadStats() {
  const request = requestState.begin('stats')
  try {
    const data = await requestKnowledgeStats(request.backend, request.settings)
    if (!request.isCurrent()) return
    knowledgeCount.value = data.total_chunks ?? data.totalChunks ?? '-'
  } catch {
    if (request.isCurrent()) knowledgeCount.value = '-'
  }
}

async function loadMonitor() {
  const request = requestState.begin('monitor')
  try {
    const data = await requestMonitor(request.backend, request.settings)
    if (request.isCurrent()) monitorData.value = { ...data, loaded: true }
  } catch {
    if (request.isCurrent()) monitorData.value = emptyMonitor()
  }
}

async function loadSkills() {
  const request = requestState.begin('skills')
  try {
    const data = await requestSkills(request.backend, request.settings)
    if (request.isCurrent()) skillsData.value = data
  } catch {
    if (request.isCurrent()) skillsData.value = { count: 0, skills: [], errors: [] }
  }
}

async function reloadSkillSet() {
  const request = requestState.begin('action')
  busy.value = true
  try {
    const data = await reloadSkills(request.backend, request.settings)
    if (!request.isCurrent()) return
    skillsData.value = data
    showToast('Skills 已重新加载')
  } catch (error) {
    if (!request.isCurrent()) return
    statusText.value = error.message
    showToast('Skills 加载失败')
  } finally { if (request.isCurrent()) busy.value = false }
}

async function sendMessage() {
  const content = draft.value.trim()
  if (!content || busy.value) return
  messages.value.push({ id: createMessageId(), role: 'user', content })
  draft.value = ''
  lastResponse.value = null
  lastTrace.value = null
  nextTick(() => messageList.value?.scrollTo({ top: messageList.value.scrollHeight, behavior: 'smooth' }))
  const request = requestState.begin('action')
  busy.value = true
  try {
    streamStatus.value = ''
    if (!settings.conversationId) {
      settings.conversationId = crypto.randomUUID()
      persist()
    }
    const response = await requestChatStream(request.backend, { ...request.settings, conversationId: settings.conversationId }, content, (event, data) => {
      // 执行过程中实时到达的事件（不是完成后回放）。
      const labels = {
        status: '已受理，正在识别问题', intent: `已识别：${data.intent}`, routed: `交给 ${(data.agent_types || []).join(' + ')} 处理`,
        plan: '已生成处理计划', step_started: `步骤开始：${data.title || data.step_id}`, step_finished: `步骤完成：${data.step_id}`,
        tool_started: `正在调用工具 ${data.tool}`, tool_finished: `工具 ${data.tool} ${data.success ? '完成' : '失败'}（${data.latency_ms}ms）`,
        approval_required: `已提交审批，需 ${data.approvals_required} 人批准`, content_delta: '正在输出回复', resumed: '连接中断，正在续传'
      }
      streamStatus.value = labels[event] || streamStatus.value
    })
    if (!request.isCurrent()) return
    if (response.conversationId && !settings.conversationId) {
      settings.conversationId = response.conversationId
      persist()
    }
    const trace = await loadToolTrace(response.requestId, request)
    if (!request.isCurrent()) return
    lastResponse.value = response
    lastTrace.value = trace
    const intentLabels = { greeting: '打招呼', chitchat: '闲聊', out_of_scope: '超出范围', vpn_issue: 'VPN 故障', password_reset: '密码重置', permission_request: '权限申请', other: '其他' }
    const intentLabel = intentLabels[response.intent] || response.intent || '其他'
    const confidence = Math.round(Math.max(0, Math.min(1, Number(response.intentConfidence ?? response.routingConfidence ?? 0))) * 100)
    const intentMeta = `${intentLabel} · 置信度 ${confidence}%`
    const clarificationMeta = response.clarificationNeeded ? `待补充：${(response.missingSlots || []).join('、')}` : ''
    const planMeta = response.plan?.steps?.length ? `计划 ${response.plan.steps.length} 步` : ''
    const approvalMeta = response.approvalRequired ? `待审批 · ${response.approvalId || ''}` : ''
    const meta = [intentMeta, clarificationMeta, planMeta, approvalMeta, response.primaryAgent || response.agentType, response.knowledgeUsed ? 'RAG' : '', response.escalated ? '转人工' : ''].filter(Boolean).join(' · ')
    messages.value.push({ id: createMessageId(), role: 'assistant', content: response.response, meta, trace: lastTrace.value?.trace || null,
      approvalId: response.approvalId, approvalSettings: request.settings, approvalBackend: request.backend })
    await loadMonitor()
  } catch (error) {
    if (!request.isCurrent()) return
    messages.value.push({ id: createMessageId(), role: 'assistant', content: error.message, meta: '请求失败', error: true })
  } finally {
    if (!request.isCurrent()) return
    busy.value = false
    streamStatus.value = ''
    await nextTick()
    messageList.value?.scrollTo({ top: messageList.value.scrollHeight, behavior: 'smooth' })
  }
}

function usePrompt(prompt) { draft.value = prompt; nextTick(() => composerInput.value?.focus()) }

async function handleApproval(item, decision) {
  if (item.approvalBusy) return
  item.approvalBusy = true
  item.approvalError = ''
  try {
    // Keep the original endpoint: switching connections must never approve an unrelated request.
    if (backendMeta(item.approvalBackend, settings).baseUrl !== backendMeta(item.approvalBackend, item.approvalSettings).baseUrl) {
      throw new Error('请切回生成此申请的服务连接后再审批。')
    }
    // 以当前登录身份操作；审批权限与职责分离由服务端判定。
    const target = { ...item.approvalSettings, accessToken: settings.accessToken }
    item.approval = decision === 'reconcile'
      ? await reconcileApproval(item.approvalBackend, target, item.approvalId)
      : await requestApproval(item.approvalBackend, target, item.approvalId, decision, item.approval?.payload_hash)
  } catch (error) {
    item.approvalError = error.message
  } finally {
    item.approvalBusy = false
  }
}

function clearConversation() {
  requestState.invalidate()
  busy.value = false
  messages.value = []
  lastResponse.value = null
  lastTrace.value = null
  settings.conversationId = ''
  persist()
}

async function searchKnowledge() {
  if (!searchQuery.value.trim() || busy.value) return
  const request = requestState.begin('action')
  busy.value = true
  try {
    const data = await requestSearch(request.backend, request.settings, searchQuery.value, 5)
    if (!request.isCurrent()) return
    searchResults.value = data.results || []
    searched.value = true
    showToast(`检索完成，返回 ${searchResults.value.length} 条结果`)
  } catch (error) {
    if (!request.isCurrent()) return
    statusText.value = error.message
    showToast('检索失败，请检查连接')
  } finally { if (request.isCurrent()) busy.value = false }
}

async function submitKnowledge() {
  if (!docTitle.value.trim() || !docContent.value.trim() || busy.value) return
  const request = requestState.begin('action')
  busy.value = true
  try {
    const data = await addKnowledge(request.backend, request.settings, [{ title: docTitle.value.trim(), content: docContent.value.trim() }])
    if (!request.isCurrent()) return
    statusText.value = JSON.stringify(data, null, 2)
    await loadStats()
    if (!request.isCurrent()) return
    showToast('文档已添加')
  } catch (error) {
    if (!request.isCurrent()) return
    statusText.value = error.message
    showToast('文档导入失败')
  } finally { if (request.isCurrent()) busy.value = false }
}

async function handleUpload(event) {
  const file = event.target.files?.[0]
  event.target.value = ''
  if (!file || busy.value) return
  if (file.size > 10 * 1024 * 1024) { showToast('文件不能超过 10 MB'); return }
  const request = requestState.begin('action')
  busy.value = true
  try {
    const data = await uploadKnowledge(request.backend, request.settings, file)
    if (!request.isCurrent()) return
    statusText.value = JSON.stringify(data, null, 2)
    await loadStats()
    if (!request.isCurrent()) return
    showToast(`${file.name} 导入成功`)
  } catch (error) {
    if (!request.isCurrent()) return
    statusText.value = error.message
    showToast('文件导入失败')
  } finally { if (request.isCurrent()) busy.value = false }
}

async function loadReports() {
  const request = requestState.begin('reports')
  try {
    const data = await requestEvaluationReports(request.backend, request.settings)
    if (request.isCurrent()) evalReports.value = data
  } catch {
    if (request.isCurrent()) evalReports.value = null
  }
}

async function loadOperations() {
  await Promise.allSettled([loadMonitor(), loadReports()])
}

async function loadToolTrace(requestId, request = requestState.begin('trace')) {
  try {
    return await requestToolTrace(request.backend, request.settings, requestId)
  } catch {
    return null
  }
}

function formatPercent(value) {
  const number = Number(value || 0)
  return `${(number <= 1 ? number * 100 : number).toFixed(1)}%`
}

function formatMetric(metric) {
  if (metric.value === null || metric.value === undefined) return '—'
  return metric.format === 'percent' ? formatPercent(metric.value) : String(metric.value)
}

function formatTime(iso) {
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleTimeString()
}

function formatJson(value) {
  try {
    return JSON.stringify(value ?? {}, null, 2)
  } catch {
    return String(value ?? '')
  }
}

function formatBody(text) {
  return String(text ?? '').replace(/\*\*([^*]+)\*\*/g, '$1')
}

function createMessageId() {
  messageSequence += 1
  return `message-${Date.now()}-${messageSequence}`
}

function showToast(message) {
  toast.value = message
  clearTimeout(toastTimer)
  toastTimer = setTimeout(() => { toast.value = '' }, 2600)
}
</script>
