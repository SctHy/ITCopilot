// Keyed by connection + token in App, so a new identity always gets a fresh inbox.
import { useMemo, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Button } from '@/components/ui/button'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { RefreshCwIcon, ShieldCheckIcon } from 'lucide-react'
import { useStore } from '../hooks/useStore.ts'
import { listApprovals } from '../api/approvals.ts'
import { ApprovalCard } from '../components/ApprovalCard.tsx'
import { Empty, ErrorAlert, PageHeader } from '../components/ui.tsx'
import { reactive } from '../lib/store.ts'
import type { Approval, ApprovalDecision, ApprovalItem, Identity, Settings } from '../lib/types.ts'

const filters = [{ id: 'draft', label: '待我确认' }, { id: 'pending', label: '待审批' }, { id: 'approved', label: '已批准' }, { id: 'rejected', label: '已拒绝' }, { id: 'withdrawn', label: '已撤回' }]

export function ApprovalsView({ settings, identity, onApproval }: {
  settings: Settings; identity: Identity | null
  onApproval: (target: ApprovalItem, decision?: ApprovalDecision, note?: string) => Promise<void>
}) {
  const [filter, setFilter] = useState('pending')
  const queryClient = useQueryClient()
  const snapshot = useMemo(() => ({ ...settings, endpoints: { ...settings.endpoints } }), [])
  const result = useQuery({ queryKey: ['approvals', filter], enabled: !!identity && !!snapshot.accessToken,
    queryFn: ({ signal }) => listApprovals(snapshot.backend, snapshot, filter, signal) as Promise<{ items: Approval[] }>,
    // Keep a card's busy/error state stable while the user is making a decision.
    refetchOnWindowFocus: false,
  })
  // Query owns server snapshots; controllers mutate separate card state, never the shared cache.
  const inbox = useStore(useMemo(() => reactive({ items: (result.isError ? [] : result.data?.items || []).map(approval => ({
    approvalId: approval.approval_id, approval: structuredClone(approval), approvalBackend: snapshot.backend, approvalSettings: snapshot,
  } as ApprovalItem)) }), [result.data, result.isError]))
  const deciding = inbox.items.some(item => item.approvalBusy)
  const loading = result.isFetching
  async function decide(item: ApprovalItem, decision?: ApprovalDecision, note?: string) {
    await onApproval(item, decision, note)
    if (!item.approvalError) await queryClient.invalidateQueries({ queryKey: ['approvals'] })
  }

  const label = filters.find(option => option.id === filter)?.label
  return (
    <section className="mx-auto w-full max-w-4xl px-4 py-8 sm:px-8">
      <PageHeader title="审批"
        description="审批人看到全部待审批申请，员工只看到本人的申请。Agent 起草的申请需申请人确认提交后才进入审批。批准后由执行器写入目标系统，到期自动回收。"
        actions={<Button type="button" variant="secondary" aria-busy={loading} onClick={() => result.refetch()} disabled={loading || deciding}><RefreshCwIcon className="size-4" />刷新</Button>} />
      <Tabs value={filter} onValueChange={value => setFilter(String(value))} className="mb-5 max-w-full overflow-x-auto pb-1">
        <TabsList aria-label="审批状态筛选">
          {filters.map(option => <TabsTrigger key={option.id} value={option.id} disabled={deciding}>{option.label}</TabsTrigger>)}
        </TabsList>
      </Tabs>
      {result.isError && <ErrorAlert>审批列表加载失败：{result.error.message}</ErrorAlert>}
      {result.isPending && !!identity ? <p role="status" className="text-sm text-muted-foreground">正在加载审批…</p> : !identity ? <Empty title="登录后查看审批" /> : !inbox.items.length && !result.isError ? (
        <Empty icon={<ShieldCheckIcon />} title={`没有${label}的申请`} description="切换上方的状态筛选，或稍后刷新查看最新申请。" />
      ) : (
        <div className="grid gap-4">
          {inbox.items.map(item => (
            <ApprovalCard key={item.approvalId} approval={item.approval} busy={item.approvalBusy} error={item.approvalError} identity={identity}
              onAction={(decision, note) => decide(item, decision, note)} />
          ))}
        </div>
      )}
    </section>
  )
}
