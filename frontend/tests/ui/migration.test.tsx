import { useState } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryHistory, RouterProvider } from '@tanstack/react-router'
import { QueryClientProvider } from '@tanstack/react-query'
import { createAppRouter } from '../../src/router'
import { createServerQueryClient } from '../../src/lib/queryClient'
import { SelectInput, MultiSelectInput } from '../../src/components/ui'
import { ApprovalsView } from '../../src/views/ApprovalsView'
import { applyApprovalAction } from '../../src/lib/approvalDecision'

const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } })
const person = (username: string) => ({ username, subject: username, department: 'IT', roles: ['employee', 'approver'] })
const summary = (username: string) => ({ conv_id: username + '-chat', title: username + '的私人记录', message_count: 2, updated_at: Date.now() / 1000 })
let router: ReturnType<typeof createAppRouter>
let fetchMock: ReturnType<typeof vi.fn<typeof fetch>>
beforeEach(() => {
  fetchMock = vi.fn(async (input: Parameters<typeof fetch>[0], init?: RequestInit) => {
    const path = new URL(String(input), 'http://localhost').pathname
    const token = new Headers(init?.headers).get('Authorization')?.replace('Bearer ', '') || 'alice'
    if (path === '/health') return json({ status: 'ok' })
    if (path === '/auth/config') return json({ mode: 'dev', users: [person('alice'), person('bob')] })
    if (path === '/auth/dev-token') return json({ access_token: JSON.parse(String(init?.body)).username })
    if (path === '/auth/me') return json(person(token))
    if (path === '/monitor') return json({ agent: {}, tools: {}, monitor: null, mcp: { servers: [] } })
    if (path === '/conversations') return json({ items: [summary(token)], has_more: false })
    if (path === '/task-inbox' || path === '/tasks') return json({ items: [], total: 0, has_more: false })
    if (path === '/approvals') return json({ items: [] })
    return json({ detail: `Unexpected endpoint ${path}` }, 404)
  })
  vi.stubGlobal('fetch', fetchMock)
})
async function openApp(path: string) {
  router = createAppRouter(createMemoryHistory({ initialEntries: [path] }))
  render(<RouterProvider router={router} />)
  await screen.findByText('alice', { exact: true })
}

describe('upstream component integration', () => {
  it('single select resolves labels; multi-select preserves several choices', async () => {
    const onSingle = vi.fn(), onMulti = vi.fn()
    function Form() {
      const [values, setValues] = useState(['alice'])
      return <><SelectInput label="处理状态" value="pending" onChange={onSingle} options={[{ id: 'pending', label: '待审批' }, { id: 'approved', label: '已批准' }]} />
        <MultiSelectInput label="参与人" values={values} onChange={next => { onMulti(next); setValues(next) }} options={[{ id: 'alice', label: 'Alice' }, { id: 'bob', label: 'Bob' }]} /></>
    }
    render(<Form />)
    expect(within(screen.getByRole('combobox', { name: '处理状态' })).getByText('待审批')).toBeTruthy()
    await userEvent.click(screen.getByRole('combobox', { name: '参与人' }))
    expect((await screen.findByRole('listbox')).getAttribute('aria-multiselectable')).toBe('true')
    await userEvent.click(screen.getByRole('option', { name: 'Bob' }))
    expect(onMulti).toHaveBeenCalledWith(['alice', 'bob'])
    expect(screen.getByRole('option', { name: 'Bob' }).getAttribute('aria-selected')).toBe('true')
    await userEvent.click(screen.getByRole('option', { name: 'Alice' }))
    expect(onMulti).toHaveBeenLastCalledWith(['bob'])
    await userEvent.keyboard('{Escape}')
    await userEvent.click(screen.getByRole('combobox', { name: '处理状态' }))
    await userEvent.click(await screen.findByRole('option', { name: '已批准' }))
    expect(onSingle).toHaveBeenCalledWith('approved')
  })

  it('Escape dismisses a nested select before its dialog and restores select focus', async () => {
    await openApp('/chat')
    await userEvent.click(screen.getByRole('button', { name: '连接配置' }))
    const dialog = await screen.findByRole('dialog')
    const trigger = within(dialog).getByRole('combobox', { name: '演示身份' })
    await userEvent.click(trigger)
    await screen.findByRole('listbox')
    await userEvent.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('listbox')).toBeNull())
    expect(screen.getByRole('dialog')).toBe(dialog)
    await waitFor(() => expect(document.activeElement).toBe(trigger))
    await userEvent.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  it('mobile sidebar opens as a dialog and closes after navigation', async () => {
    const originalMatchMedia = vi.mocked(window.matchMedia).getMockImplementation()!
    vi.spyOn(window, 'matchMedia').mockImplementation(query => ({
      ...originalMatchMedia(query), matches: query === '(max-width: 767px)',
    }))
    try {
      router = createAppRouter(createMemoryHistory({ initialEntries: ['/chat'] }))
      render(<RouterProvider router={router} />)
      await screen.findByRole('textbox', { name: '员工 IT 问题' })
      await userEvent.click(screen.getByRole('button', { name: 'Toggle Sidebar' }))
      const sidebar = await screen.findByRole('dialog', { name: 'Sidebar' })
      await userEvent.click(within(sidebar).getByRole('button', { name: /^审批$/ }))
      await screen.findByRole('heading', { name: '审批' })
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
      expect(router.state.location.pathname).toBe('/approvals')
    } finally {
      vi.mocked(window.matchMedia).mockImplementation(originalMatchMedia)
    }
  })

  it('routes pages and restores the selected page on browser back', async () => {
    await openApp('/chat')
    await userEvent.click(within(screen.getByRole('navigation', { name: '主导航' })).getByRole('button', { name: '审批' }))
    await screen.findByRole('heading', { name: '审批' })
    expect(router.state.location.pathname).toBe('/approvals')
    await act(async () => { router.history.back() })
    await screen.findByRole('textbox', { name: '员工 IT 问题' })
    expect(router.state.location.pathname).toBe('/chat')
  })

  it('keeps current task links and new-task URLs addressable', async () => {
    await openApp('/tasks?task=room-42')
    expect(router.state.location.pathname).toBe('/tasks')
    expect(router.state.location.search.task).toBe('room-42')
    await act(async () => { await router.navigate({ to: '/tasks', search: { new: true } }) })
    await screen.findByText('发起共享事项')
    expect(screen.getByRole('textbox', { name: /事项名称/ })).toBeTruthy()
    await act(async () => { router.history.back() })
    await waitFor(() => expect(router.state.location.search.task).toBe('room-42'))
    await waitFor(() => expect(screen.queryByText('发起共享事项')).toBeNull())
  })

  it('mounts a fresh private-data cache when switching identity', async () => {
    await openApp('/chat')
    await userEvent.click(screen.getByRole('button', { name: '连接配置' }))
    const dialog = screen.getByRole('dialog')
    await userEvent.click(within(dialog).getByRole('combobox', { name: '演示身份' }))
    await userEvent.click(await screen.findByRole('option', { name: /bob/ }))
    await userEvent.click(within(dialog).getByRole('button', { name: '切换身份' }))
    await screen.findByText('bob的私人记录')
    expect(screen.queryByText('alice的私人记录')).toBeNull()
  })

  it('discards an old identity response that arrives after the switch', async () => {
    const normalFetch = fetchMock.getMockImplementation()!
    let finishOld!: (response: Response) => void
    let oldSignal: AbortSignal | null | undefined
    fetchMock.mockImplementation((input, init) => {
      if (new URL(String(input), 'http://localhost').pathname === '/conversations' && new Headers(init?.headers).get('Authorization') === 'Bearer alice') {
        oldSignal = init?.signal
        return new Promise<Response>(resolve => { finishOld = resolve })
      }
      return normalFetch(input, init)
    })
    await openApp('/chat')
    await waitFor(() => expect(finishOld).toBeTypeOf('function'))
    await userEvent.click(screen.getByRole('button', { name: '连接配置' }))
    const dialog = screen.getByRole('dialog')
    await userEvent.click(within(dialog).getByRole('combobox', { name: '演示身份' }))
    await userEvent.click(await screen.findByRole('option', { name: /bob/ }))
    await userEvent.click(within(dialog).getByRole('button', { name: '切换身份' }))
    await screen.findByText('bob的私人记录')
    expect(oldSignal?.aborted).toBe(true)
    await act(async () => { finishOld(json({ items: [summary('alice')], has_more: false })) })
    expect(screen.queryByText('alice的私人记录')).toBeNull()
    expect(screen.getByText('bob的私人记录')).toBeTruthy()
  })

  it('does not reopen a late history response after starting a new conversation', async () => {
    const normalFetch = fetchMock.getMockImplementation()!
    let finishHistory!: (response: Response) => void
    fetchMock.mockImplementation((input, init) => {
      if (new URL(String(input), 'http://localhost').pathname === '/conversations/alice-chat') {
        return new Promise<Response>(resolve => { finishHistory = resolve })
      }
      return normalFetch(input, init)
    })
    await openApp('/chat')
    await userEvent.click(await screen.findByText('alice的私人记录'))
    await waitFor(() => expect(finishHistory).toBeTypeOf('function'))
    await userEvent.click(screen.getAllByRole('button', { name: /^新对话$/ })[0]!)
    await act(async () => { finishHistory(json({ messages: [{ request_id: 'old', role: 'user', content: '不应恢复的旧消息', meta: {} }] })) })
    expect(screen.queryAllByText('不应恢复的旧消息')).toHaveLength(0)
    expect(JSON.parse(localStorage.getItem('it-copilot.frontend.settings')!).conversationId).toBe('')
  })

  it('renders an unknown URL as a missing page', async () => {
    await openApp('/does-not-exist')
    expect(screen.getByRole('heading', { name: '页面不存在' })).toBeTruthy()
  })

  it('retains the original approval hash and refreshes the inbox after a decision', async () => {
    let approved = false
    const approval = { approval_id: 'approval-1', status: 'pending', requester: 'employee', payload_hash: 'reviewed-hash', approvals_required: 1 }
    fetchMock.mockImplementation(async (_input, init) => {
      if (init?.method === 'POST') { approved = true; return json({ ...approval, status: 'approved' }) }
      return json({ items: approved ? [] : [approval] })
    })
    const settings = { backend: 'python', endpoints: { python: 'http://localhost' }, accessToken: 'reviewer', conversationId: '' }
    const client = createServerQueryClient()
    render(<QueryClientProvider client={client}><ApprovalsView settings={settings} identity={person('reviewer') as any}
      onApproval={(item, decision, note) => applyApprovalAction(item, settings, decision, note)} /></QueryClientProvider>)
    await userEvent.click(await screen.findByRole('button', { name: '同意审批' }))
    await screen.findByText('没有待审批的申请')
    const write = fetchMock.mock.calls.find(([, init]) => init?.method === 'POST')!
    expect(JSON.parse(String(write[1]?.body))).toMatchObject({ decision: 'approved', payload_hash: 'reviewed-hash' })
    expect(new Headers(write[1]?.headers).get('Authorization')).toBe('Bearer reviewer')
    client.clear()
  })
})
