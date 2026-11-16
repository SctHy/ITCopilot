import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import { TaskWorkspace } from '../../src/components/task/TaskWorkspace'
import { createTaskController } from '../../src/lib/taskController'
import { reactive } from '../../src/lib/store'
import { recordAudio } from '../../src/audio/recorder'
import { requestFor } from '../../src/api/http'
vi.mock('@tanstack/react-router', () => ({ useNavigate: () => vi.fn() }))
vi.mock('../../src/lib/taskClient', () => ({ createTaskClient: vi.fn() }))
vi.mock('../../src/lib/taskController', () => ({ createTaskController: vi.fn() }))
vi.mock('../../src/components/task/TaskConversation', () => ({ TaskConversation: () => null }))
vi.mock('../../src/components/task/TaskDetails', () => ({ TaskDetails: () => null }))
vi.mock('../../src/components/task/TaskCollaboration', () => ({ TaskCollaborationProvider: ({ children }: any) => children, TaskNextActions: () => null, TaskParticipantsButton: () => null }))
vi.mock('../../src/audio/recorder', () => ({ recordAudio: vi.fn() }))
vi.mock('../../src/api/http', () => ({ requestFor: vi.fn() }))
const settings = { backend: 'python', endpoints: {}, conversationId: '' }
let state: any, controller: any
const cancel = vi.fn()
beforeEach(() => {
  vi.clearAllMocks()
  state = reactive({ selected: '', creating: false, room: null, query: {}, busy: false, title: '报障', initialMessage: '已有描述', message: '已换网络', approvalBusy: {}, approvalErrors: {} })
  controller = { state, refresh: vi.fn(), dispose: vi.fn(), send: vi.fn(), create: vi.fn() }
  vi.mocked(createTaskController).mockReturnValue(controller)
  vi.mocked(recordAudio).mockResolvedValue({ stop: vi.fn(async () => new Blob(['wav'])), cancel })
  vi.mocked(requestFor).mockResolvedValue({ text: '仍然连接失败' })
})
function room(id = 'one', can_write = true) {
  return { id, title: 'VPN 报障', status: 'open', phase: 'waiting', can_write, events: [], runs: [] }
}
async function start() {
  fireEvent.click(screen.getByRole('button', { name: '开始语音输入' }))
  await screen.findByRole('button', { name: '停止并识别' })
}
it('adds speech to new-task description without creating the task', async () => {
  render(<TaskWorkspace settings={settings} identity={null} requestedTask={{ id: '', at: 1 }} />)
  await start()
  expect((screen.getByRole('button', { name: '创建并请 Agent 处理' }) as HTMLButtonElement).disabled).toBe(true)
  fireEvent.click(screen.getByRole('button', { name: '停止并识别' }))
  await waitFor(() => expect(state.initialMessage).toBe('已有描述\n仍然连接失败'))
  expect(controller.create).not.toHaveBeenCalled()
})
it('updates discussion draft only and cancels on loss of write access', async () => {
  state.room = room()
  render(<TaskWorkspace settings={settings} identity={null} requestedTask={{ id: 'one', at: 1 }} />)
  await start()
  fireEvent.click(screen.getByRole('button', { name: '停止并识别' }))
  await waitFor(() => expect(state.message).toBe('已换网络\n仍然连接失败'))
  expect(controller.send).not.toHaveBeenCalled()
  await start()
  act(() => { state.room = room('one', false) })
  expect(cancel).toHaveBeenCalled()
  expect(screen.queryByRole('button', { name: '开始语音输入' })).toBeNull()
})
it('discards an old-room transcript when switching rooms', async () => {
  let resolve!: (data: any) => void
  vi.mocked(requestFor).mockImplementation(() => new Promise(r => { resolve = r }))
  state.room = room()
  render(<TaskWorkspace settings={settings} identity={null} requestedTask={{ id: 'one', at: 1 }} />)
  await start()
  fireEvent.click(screen.getByRole('button', { name: '停止并识别' }))
  await waitFor(() => expect(requestFor).toHaveBeenCalled())
  act(() => { state.room = room('two'); state.message = '新事项草稿' })
  await act(async () => resolve({ text: '旧事项录音' }))
  expect(state.message).toBe('新事项草稿')
})
