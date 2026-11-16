import { useState } from 'react'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { SpeechInput } from '../../src/components/SpeechInput'
import { recordAudio } from '../../src/audio/recorder'
import { requestFor } from '../../src/api/http'

vi.mock('../../src/audio/recorder', () => ({ recordAudio: vi.fn() }))
vi.mock('../../src/api/http', () => ({ requestFor: vi.fn() }))
const settings = { backend: 'python', endpoints: {}, accessToken: 'token', conversationId: 'one' }
const stop = vi.fn(), cancel = vi.fn()
beforeEach(() => {
  vi.clearAllMocks()
  stop.mockResolvedValue(new Blob(['wav'], { type: 'audio/wav' }))
  vi.mocked(recordAudio).mockResolvedValue({ stop, cancel })
  vi.mocked(requestFor).mockResolvedValue({ text: '申请 GitLab 权限' })
})
async function start() {
  fireEvent.click(screen.getByRole('button', { name: '开始语音输入' }))
  await screen.findByRole('button', { name: '停止并识别' })
}
it('uploads only on stop and returns text without sending a chat request', async () => {
  const onText = vi.fn(), onBusy = vi.fn()
  render(<SpeechInput settings={settings} disabled={false} onText={onText} onBusy={onBusy} />)
  await start()
  expect(requestFor).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: '停止并识别' }))
  await waitFor(() => expect(onText).toHaveBeenCalledWith('申请 GitLab 权限'))
  expect(vi.mocked(requestFor).mock.calls[0]?.[2]).toBe('/speech/transcribe')
  expect(onBusy).toHaveBeenLastCalledWith(false)
})
it('cancels recording without uploading', async () => {
  render(<SpeechInput settings={settings} disabled={false} onText={vi.fn()} onBusy={vi.fn()} />)
  await start()
  fireEvent.click(screen.getByRole('button', { name: '取消' }))
  expect(cancel).toHaveBeenCalled()
  expect(requestFor).not.toHaveBeenCalled()
  expect(vi.mocked(recordAudio).mock.calls[0]?.[0].aborted).toBe(true)
})
it('ignores a late transcript after conversation change/unmount', async () => {
  let resolve!: (value: unknown) => void
  vi.mocked(requestFor).mockImplementation(() => new Promise(r => { resolve = r }))
  const onText = vi.fn()
  const view = render(<SpeechInput settings={settings} disabled={false} onText={onText} onBusy={vi.fn()} />)
  await start()
  fireEvent.click(screen.getByRole('button', { name: '停止并识别' }))
  await waitFor(() => expect(requestFor).toHaveBeenCalled())
  view.unmount()
  await act(async () => resolve({ text: '旧会话文字' }))
  expect(onText).not.toHaveBeenCalled()
  expect(vi.mocked(requestFor).mock.calls[0]?.[3]?.signal?.aborted).toBe(true)
})
it('cleans up a microphone grant arriving after cancellation', async () => {
  let resolve!: (value: any) => void
  vi.mocked(recordAudio).mockImplementation(() => new Promise(r => { resolve = r }))
  render(<SpeechInput settings={settings} disabled={false} onText={vi.fn()} onBusy={vi.fn()} />)
  fireEvent.click(screen.getByRole('button', { name: '开始语音输入' }))
  fireEvent.click(screen.getByRole('button', { name: '取消' }))
  await act(async () => resolve({ stop, cancel }))
  expect(cancel).toHaveBeenCalled()
  expect(requestFor).not.toHaveBeenCalled()
})
it('shows an error and allows retry after recognition failure', async () => {
  vi.mocked(requestFor).mockRejectedValue(new Error('语音识别超时'))
  render(<SpeechInput settings={settings} disabled={false} onText={vi.fn()} onBusy={vi.fn()} />)
  await start()
  fireEvent.click(screen.getByRole('button', { name: '停止并识别' }))
  expect((await screen.findByRole('alert')).textContent).toContain('语音识别超时')
  expect(screen.getByRole('button', { name: '重试识别' })).toBeTruthy()
})
afterEach(() => vi.useRealTimers())
function Draft() {
  const [value, onChange] = useState('已有文字')
  return <><input aria-label="草稿" value={value} onChange={event => onChange(event.target.value)} />
    <SpeechInput settings={settings} disabled={false} value={value} onChange={onChange} onBusy={vi.fn()} /></>
}
it('undo restores the original draft and never overwrites manual edits', async () => {
  render(<Draft />)
  await start()
  fireEvent.click(screen.getByRole('button', { name: '停止并识别' }))
  fireEvent.click(await screen.findByRole('button', { name: '撤销本次转写' }))
  expect((screen.getByRole('textbox', { name: '草稿' }) as HTMLInputElement).value).toBe('已有文字')
  await start()
  fireEvent.click(screen.getByRole('button', { name: '停止并识别' }))
  await screen.findByRole('button', { name: '撤销本次转写' })
  fireEvent.change(screen.getByRole('textbox', { name: '草稿' }), { target: { value: '手动修改' } })
  expect(screen.queryByRole('button', { name: '撤销本次转写' })).toBeNull()
})
it('retries the same audio without recording again', async () => {
  vi.mocked(requestFor).mockRejectedValueOnce(new Error('网络错误')).mockResolvedValueOnce({ text: '识别成功' })
  const onText = vi.fn()
  render(<SpeechInput settings={settings} disabled={false} onText={onText} onBusy={vi.fn()} />)
  await start()
  fireEvent.click(screen.getByRole('button', { name: '停止并识别' }))
  fireEvent.click(await screen.findByRole('button', { name: '重试识别' }))
  await waitFor(() => expect(onText).toHaveBeenCalledExactlyOnceWith('识别成功'))
  expect(recordAudio).toHaveBeenCalledTimes(1)
  expect(vi.mocked(requestFor).mock.calls[0]?.[3]?.body).toBe(vi.mocked(requestFor).mock.calls[1]?.[3]?.body)
})
it('expires retained audio after two minutes', async () => {
  vi.mocked(requestFor).mockRejectedValue(new Error('网络错误'))
  render(<SpeechInput settings={settings} disabled={false} onText={vi.fn()} onBusy={vi.fn()} />)
  await start()
  vi.useFakeTimers()
  await act(async () => fireEvent.click(screen.getByRole('button', { name: '停止并识别' })))
  expect(screen.getByRole('button', { name: '重试识别' })).toBeTruthy()
  await act(async () => vi.advanceTimersByTime(120000))
  expect(screen.queryByRole('button', { name: '重试识别' })).toBeNull()
  expect(screen.getByRole('alert').textContent).toContain('录音已释放')
})
it('shows silence, volume and near-limit warning then stops automatically', async () => {
  render(<SpeechInput settings={settings} disabled={false} onText={vi.fn()} onBusy={vi.fn()} />)
  vi.useFakeTimers()
  await act(async () => fireEvent.click(screen.getByRole('button', { name: '开始语音输入' })))
  await act(async () => vi.advanceTimersByTime(5000))
  expect(screen.getByText(/近期未检测到明显声音/)).toBeTruthy()
  act(() => vi.mocked(recordAudio).mock.calls[0]?.[2]?.(0.5))
  expect(screen.queryByText(/近期未检测到明显声音/)).toBeNull()
  expect((screen.getByRole('meter') as HTMLMeterElement).value).toBe(0.5)
  await act(async () => vi.advanceTimersByTime(100000))
  expect(screen.getByText(/还剩 15 秒/)).toBeTruthy()
  await act(async () => vi.advanceTimersByTime(15000))
  expect(stop).toHaveBeenCalledTimes(1)
})
it('disabling cancels active recording', async () => {
  const props = { settings, onText: vi.fn(), onBusy: vi.fn() }
  const view = render(<SpeechInput {...props} disabled={false} />)
  await start()
  view.rerender(<SpeechInput {...props} disabled />)
  expect(cancel).toHaveBeenCalled()
  expect(requestFor).not.toHaveBeenCalled()
})
it('rerecording restores the draft before the last transcription', async () => {
  render(<Draft />)
  await start()
  fireEvent.click(screen.getByRole('button', { name: '停止并识别' }))
  fireEvent.click(await screen.findByRole('button', { name: '重新录音' }))
  await screen.findByRole('button', { name: '停止并识别' })
  expect((screen.getByRole('textbox', { name: '草稿' }) as HTMLInputElement).value).toBe('已有文字')
  expect(recordAudio).toHaveBeenCalledTimes(2)
})
