// TanStack Query owns the shared monitor cache and its 15s polling in App.
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { ActivityIcon, CpuIcon, RefreshCwIcon, TriangleAlertIcon } from 'lucide-react'
import { Empty, Gauge, PageHeader, Panel, StatusChip, type Tone } from '../components/ui.tsx'
import { formatPercent, formatTime } from '../lib/format.ts'
import type { Identity, MonitorData, MonitorSummary, Settings } from '../lib/types.ts'

const sloLabels: Record<string, string> = { chat_availability: '可用性', chat_latency: '延迟达标率' }
const statusMap: Record<string, { label: string; tone: Tone }> = { ok: { label: '正常', tone: 'success' }, degraded: { label: '降级', tone: 'warning' }, critical: { label: '严重', tone: 'danger' } }
const toneText: Record<Tone, string> = { success: 'text-success', warning: 'text-warning', danger: 'text-destructive-foreground', accent: 'text-accent', default: 'text-foreground' }

function Stat({ label, value, tone = 'default', hint }: { label: string; value: string | number; tone?: Tone; hint: string }) {
  return (
    <Card className="gap-1 p-5">
      <p className="text-[13px] text-muted-foreground">{label}</p>
      <p className={`text-2xl font-semibold tabular-nums ${toneText[tone]}`}>{value}</p>
      <p className="text-[13px] text-muted-foreground">{hint}</p>
    </Card>
  )
}

export function OperationsView({ monitorData, onRefresh }: {
  monitorData: MonitorData; settings: Settings; identity: Identity | null; onRefresh: () => void
}) {
  const summary: MonitorSummary = monitorData.monitor || { status: 'unknown', slos: [], alerts: [], recent_resolved: [], last_tick: null }
  const status = statusMap[summary.status] || { label: monitorData.loaded ? '未知' : '未连接', tone: 'default' as Tone }
  const agent = monitorData.agent || {}

  return (
    <section className="mx-auto w-full max-w-6xl px-4 py-8 sm:px-8">
      <PageHeader title="运行状态" description="实时 SLO 燃烧率、告警与 Agent 健康度，每 15 秒自动刷新。"
        actions={<Button type="button" variant="secondary" onClick={onRefresh}><RefreshCwIcon className="size-4" />刷新</Button>} />
        <div className="flex flex-col gap-5">
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <Stat label="服务状态" value={status.label} tone={status.tone} hint={summary.last_tick ? `最近评估 ${formatTime(summary.last_tick)}` : '等待监控首次评估'} />
            {summary.slos.map(slo => {
              const burn = slo.burn_rate['1h'] ?? 0
              return <Stat key={slo.name} label={`${sloLabels[slo.name] || slo.name} · 1h 燃烧率`} value={`${burn}x`} tone={burn > 6 ? 'danger' : 'success'}
                hint={`目标 ${formatPercent(slo.target)} · 近 1h ${slo.requests_1h} 次 / ${slo.bad_1h} 次不达标`} />
            })}
            <Stat label="触发中的告警" value={summary.alerts.length} tone={summary.alerts.length ? 'danger' : 'success'} hint={`近期已恢复 ${summary.recent_resolved.length} 条`} />
          </div>
          <div className="grid gap-5 lg:grid-cols-2">
            <Panel title="告警与处置建议" icon={<TriangleAlertIcon />}>
              {!summary.alerts.length ? <Empty icon={<ActivityIcon />} title="当前没有触发中的告警" description="SLO 燃烧率超过阈值时，会在这里给出处置建议。" /> : (
                <div className="divide-y divide-border">
                  {summary.alerts.map(alert => (
                    <div key={alert.key} className="py-4 first:pt-0 last:pb-0">
                      <div className="flex items-center gap-2">
                        <StatusChip tone={alert.severity === 'critical' ? 'danger' : 'warning'}>{alert.severity === 'critical' ? '严重' : '警告'}</StatusChip>
                        <code className="text-[13px]">{alert.key}</code>
                      </div>
                      <p className="mt-2 text-sm">{alert.message}</p>
                      {alert.action && <p className="mt-1 text-[13px] text-muted-foreground">{alert.action}</p>}
                    </div>
                  ))}
                </div>
              )}
            </Panel>
            <Panel title="组件健康度" icon={<CpuIcon />}>
              {agent.model && (
                <Gauge className="mb-4" value={agent.recent_total ? (agent.recent_success_rate ?? 0) * 100 : 0}
                  tone={(agent.recent_success_rate ?? 1) < 0.9 ? 'warning' : 'success'}
                  label={`Agent · ${agent.model} 近期成功率`} valueText={agent.recent_total ? formatPercent(agent.recent_success_rate) : '—'} />
              )}
              <ul className="divide-y divide-border">
                {Object.entries(monitorData.tools || {}).map(([name, tool]) => (
                  <li key={name} className="flex items-center justify-between gap-3 py-2.5 text-sm">
                    <code className="truncate text-[13px]">{name}</code>
                    <StatusChip tone={tool.circuit_state === 'closed' ? 'success' : 'danger'}>{tool.circuit_state}</StatusChip>
                  </li>
                ))}
                {(monitorData.mcp?.servers || []).map(server => (
                  <li key={server.name} className="flex items-center justify-between gap-3 py-2.5 text-sm">
                    <span className="truncate">MCP · {server.name}<span className="ml-1 text-[13px] text-muted-foreground">（{server.tools.length} 个工具）</span></span>
                    <StatusChip tone={server.state === 'connected' ? 'success' : 'danger'}>{server.state}</StatusChip>
                  </li>
                ))}
              </ul>
              {!monitorData.mcp?.servers?.length && <p className="mt-2 text-[13px] text-muted-foreground">未启用外部 MCP 服务。</p>}
            </Panel>
          </div>
        </div>
    </section>
  )
}
