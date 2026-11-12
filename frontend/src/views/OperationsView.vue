<template>
  <section class="page page-evaluation">
    <div class="page-heading">
      <div class="heading-copy">
        <span class="kicker">OPERATIONS</span>
        <h1>运行状态</h1>
        <p>实时 SLO 燃烧率、告警与 Agent 健康度。</p>
      </div>
      <button @click="emit('refresh')">
        <AppIcon name="refresh" />
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
          <strong :class="(slo.burn_rate['1h'] ?? 0) > 6 ? 'danger' : 'success'">{{ slo.burn_rate['1h'] }}x</strong>
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
            <div v-if="monitorData.agent?.model">
              <span>Agent · {{ monitorData.agent.model }}</span>
              <i>
                <b :style="{ width: `${monitorData.agent.recent_total ? (monitorData.agent.recent_success_rate ?? 0) * 100 : 0}%` }"></b>
              </i>
              <strong>{{ monitorData.agent.recent_total ? formatPercent(monitorData.agent.recent_success_rate) : '—' }}</strong>
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
    </div>
  </section>
</template>

<script setup lang="ts">
// Monitor data is owned by App.vue (the chat inspector shows it too); this view refreshes it on entry and every 15s.
import { computed, onBeforeUnmount, onMounted } from 'vue'
import AppIcon from '../components/AppIcon.vue'
import { createPolling } from '../lib/stateMachine.ts'
import { formatPercent, formatTime } from '../lib/format.ts'
import type { MonitorData, MonitorSummary } from '../lib/types.ts'

const props = defineProps<{ monitorData: MonitorData }>()
const emit = defineEmits<{ refresh: [] }>()

const sloLabels: Record<string, string> = { chat_availability: '可用性', chat_latency: '延迟达标率' }
const monitorSummary = computed<MonitorSummary>(() => props.monitorData.monitor || { status: 'unknown', slos: [], alerts: [], recent_resolved: [], last_tick: null })
const serviceStatus = computed(() => ({
  ok: { label: '正常', className: 'success' },
  degraded: { label: '降级', className: 'warning' },
  critical: { label: '严重', className: 'danger' }
}[monitorSummary.value.status as string] || { label: props.monitorData.loaded ? '未知' : '未连接', className: '' }))

const polling = createPolling(() => emit('refresh'), 15000)
onMounted(() => { emit('refresh'); polling.start() })
onBeforeUnmount(() => polling.stop())
</script>
