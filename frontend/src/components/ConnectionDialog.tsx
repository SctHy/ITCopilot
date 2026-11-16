// Connection settings and sign-in. App applies a saved connection; this dialog only edits a draft.
import { useEffect, useState } from 'react'
import { CheckIcon, RefreshCwIcon } from 'lucide-react'
import { Avatar, AvatarFallback } from '@/components/ui/avatar'
import { Button } from '@/components/ui/button'
import { Dialog, DialogClose, DialogDescription, DialogFooter, DialogHeader, DialogContent, DialogTitle } from '@/components/ui/dialog'
import { useStore } from '../hooks/useStore.ts'
import { formatRoles } from '../lib/format.ts'
import type { AuthSession } from '../lib/authSession.ts'
import { Collapse, Mono, SelectInput, StatusChip, TextInput, errorText } from './ui.tsx'

export interface ConnectionDraft { endpoint: string; conversationId: string }
export interface HealthState { ok: boolean; label: string; detail: string }

export function ConnectionDialog({ isOpen, onOpenChange, initial, backendLabel, auth, health, onSave, onCheckHealth, onToast }: {
  isOpen: boolean; onOpenChange: (open: boolean) => void; initial: ConnectionDraft; backendLabel: string; auth: AuthSession
  health: HealthState; onSave: (draft: ConnectionDraft) => void; onCheckHealth: () => void; onToast: (message: string) => void
}) {
  const authState = useStore(auth.state)
  const [draft, setDraft] = useState<ConnectionDraft>(initial)
  const [switching, setSwitching] = useState(false)
  useEffect(() => { if (isOpen) setDraft(initial) }, [isOpen])  // eslint-disable-line react-hooks/exhaustive-deps

  const identity = authState.identity
  const config = authState.config

  function save() {
    const endpoint = draft.endpoint.trim().replace(/\/+$/, '')
    if (!endpoint || !(endpoint.startsWith('/') || /^https?:\/\//i.test(endpoint))) {
      onToast('请输入代理路径或 HTTP / HTTPS 服务地址')
      return
    }
    onSave({ endpoint, conversationId: draft.conversationId.trim() })
    onOpenChange(false)
  }

  async function switchDevUser() {
    setSwitching(true)
    try { await auth.loginAs(authState.devUser) }
    catch (error) { onToast(`切换身份失败：${errorText(error)}`) }
    finally { setSwitching(false) }
  }

  return (
    <Dialog open={isOpen} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90svh] overflow-y-auto sm:max-w-lg">
        <form className="contents" onSubmit={event => { event.preventDefault(); save() }}>
          <DialogHeader>
            <DialogTitle>连接配置</DialogTitle>
            <DialogDescription>配置 {backendLabel} 服务、登录身份与当前会话。</DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-5">
            <TextInput label="服务地址" value={draft.endpoint} onChange={endpoint => setDraft({ ...draft, endpoint })} isRequired
              placeholder="http://127.0.0.1:8001" description="使用代理路径或完整的 HTTP / HTTPS 地址。" />

            <section className="rounded-xl border bg-muted/40 p-4">
              <p className="mb-3 font-medium text-muted-foreground text-xs">当前身份</p>
              <div className="flex items-center gap-3">
                <Avatar className="size-9"><AvatarFallback>{(identity?.username || '?').slice(0, 1).toUpperCase()}</AvatarFallback></Avatar>
                <div className="min-w-0">
                  <p className="font-medium text-sm">{identity ? identity.username : '未登录'}</p>
                  {identity && <p className="text-muted-foreground text-xs">工号 {identity.employee_id || '-'} · {identity.department}</p>}
                </div>
              </div>
              {identity && (
                <div className="mt-3 flex flex-wrap items-center gap-1.5">
                  {identity.roles.length ? identity.roles.map(role => <StatusChip key={role} tone="accent">{formatRoles([role])}</StatusChip>) : <StatusChip>无角色</StatusChip>}
                  <span className="text-muted-foreground text-xs">来自已验证令牌，服务端强制执行</span>
                </div>
              )}
              {config?.mode === 'dev' && (
                <div className="mt-4 flex flex-col gap-2 sm:flex-row sm:items-end">
                  <SelectInput label="演示身份" value={authState.devUser} onChange={user => { authState.devUser = user }}
                    options={(config.users || []).map(user => ({ id: user.username, label: `${user.username} · ${user.department} · ${formatRoles(user.roles)}` }))} />
                  <Button type="button" variant="outline" className="shrink-0" aria-busy={switching} onClick={switchDevUser} disabled={switching}>切换身份</Button>
                </div>
              )}
              {config?.mode === 'oidc' && (
                <div className="mt-4">
                  {!identity
                    ? <Button type="button" onClick={() => auth.loginOidc()}>通过 Keycloak 登录</Button>
                    : <Button type="button" variant="outline" onClick={() => auth.logoutOidc()}>退出登录</Button>}
                </div>
              )}
            </section>

            <TextInput label="会话 ID" value={draft.conversationId} onChange={conversationId => setDraft({ ...draft, conversationId })} placeholder="留空以自动生成" />

            <div className="flex items-center justify-between gap-3 rounded-xl border px-4 py-3">
              <span className="flex items-center gap-2 text-sm">
                <span className={`size-2 rounded-full ${health.ok ? 'bg-success' : 'bg-destructive'}`} />
                {health.ok ? '服务已连接' : health.label}
              </span>
              <Button type="button" size="sm" variant="ghost" onClick={onCheckHealth}>检查当前连接<RefreshCwIcon /></Button>
            </div>
            {health.detail && (
              <Collapse trigger="查看连接详情" triggerClassName="-ml-2" panelClassName="pt-2"><Mono>{health.detail}</Mono></Collapse>
            )}
          </div>
          <DialogFooter>
            <DialogClose render={<Button type="button" variant="ghost" />}>取消</DialogClose>
            <Button type="submit">保存配置<CheckIcon /></Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
