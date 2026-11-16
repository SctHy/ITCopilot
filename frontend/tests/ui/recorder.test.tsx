import { beforeEach, expect, it, vi } from 'vitest'
import { encodeWav, recordAudio } from '../../src/audio/recorder'

let node: any
const trackStop = vi.fn(), close = vi.fn(async () => {}), disconnect = vi.fn()
const stream = { getTracks: () => [{ stop: trackStop }] }
beforeEach(() => {
  vi.clearAllMocks()
  vi.stubGlobal('navigator', { mediaDevices: { getUserMedia: vi.fn(async () => stream) } })
  vi.stubGlobal('AudioContext', class {
    sampleRate = 16000
    resume = async () => {}
    close = close
    audioWorklet = { addModule: async () => {} }
    createMediaStreamSource = () => ({ connect() {}, disconnect })
  })
  vi.stubGlobal('AudioWorkletNode', class {
    port = { onmessage: null as any, postMessage: () => { this.port.onmessage?.({ data: new Float32Array([0.25]) }); this.port.onmessage?.({ data: { done: true } }) } }
    connect() {}
    disconnect = disconnect
    constructor() { node = this }
  })
})
async function bytes(blob: Blob): Promise<ArrayBuffer> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(reader.result as ArrayBuffer)
    reader.onerror = reject
    reader.readAsArrayBuffer(blob)
  })
}
it('encodes clipped signed little-endian PCM with correct WAV sizes', async () => {
  const view = new DataView(await bytes(encodeWav([new Float32Array([-2, 0, 2])], 16000)))
  expect(view.getUint32(24, true)).toBe(16000)
  expect(view.getUint32(40, true)).toBe(6)
  expect(view.getInt16(44, true)).toBe(-32768)
  expect(view.getInt16(48, true)).toBe(32767)
})
it('flushes final samples before closing the microphone', async () => {
  const recording = await recordAudio(new AbortController().signal, vi.fn())
  node.port.onmessage({ data: new Float32Array([0, -1]) })
  const blob = await recording.stop()
  expect(blob.size).toBe(50)
  expect(trackStop).toHaveBeenCalled()
  expect(close).toHaveBeenCalled()
})
it('stops a microphone that is granted after abort', async () => {
  let resolve!: (value: any) => void
  vi.mocked(navigator.mediaDevices.getUserMedia).mockImplementation(() => new Promise(r => { resolve = r }))
  const controller = new AbortController()
  const pending = recordAudio(controller.signal, vi.fn())
  await Promise.resolve()
  controller.abort()
  resolve(stream)
  await expect(pending).rejects.toMatchObject({ name: 'AbortError' })
  expect(trackStop).toHaveBeenCalled()
})
it('cancels resources when worklet setup fails', async () => {
  vi.stubGlobal('AudioWorkletNode', class { constructor() { throw new Error('worklet failed') } })
  await expect(recordAudio(new AbortController().signal, vi.fn())).rejects.toThrow('worklet failed')
  expect(trackStop).toHaveBeenCalled()
  expect(close).toHaveBeenCalled()
})
it('reports sample volume and caps the recording at 120 seconds', async () => {
  const limit = vi.fn(), level = vi.fn()
  const recording = await recordAudio(new AbortController().signal, limit, level)
  node.port.onmessage({ data: new Float32Array([0, 0]) })
  expect(level).toHaveBeenLastCalledWith(0)
  node.port.onmessage({ data: new Float32Array(16000 * 121).fill(0.1) })
  expect(level.mock.calls.at(-1)?.[0]).toBeCloseTo(0.5)
  expect(limit).toHaveBeenCalledTimes(1)
  const blob = await recording.stop()
  expect(blob.size).toBe(44 + 16000 * 120 * 2)
})
