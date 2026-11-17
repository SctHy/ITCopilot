import workletUrl from './pcm-worklet.ts?worker&url'

export function encodeWav(chunks: Float32Array[], sampleRate: number): Blob {
  const count = chunks.reduce((n, chunk) => n + chunk.length, 0)
  const buffer = new ArrayBuffer(44 + count * 2)
  const view = new DataView(buffer)
  const label = (offset: number, text: string) => [...text].forEach((c, i) => view.setUint8(offset + i, c.charCodeAt(0)))
  label(0, 'RIFF'); view.setUint32(4, 36 + count * 2, true); label(8, 'WAVE'); label(12, 'fmt ')
  view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true)
  view.setUint32(24, sampleRate, true); view.setUint32(28, sampleRate * 2, true)
  view.setUint16(32, 2, true); view.setUint16(34, 16, true); label(36, 'data'); view.setUint32(40, count * 2, true)
  let offset = 44
  for (const chunk of chunks) for (const sample of chunk) {
    const value = Math.max(-1, Math.min(1, sample))
    view.setInt16(offset, Math.round(value * (value < 0 ? 32768 : 32767)), true)
    offset += 2
  }
  return new Blob([buffer], { type: 'audio/wav' })
}

export interface Recording { stop: () => Promise<Blob>; cancel: () => void }

export async function recordAudio(signal: AbortSignal, onLimit: () => void, onLevel: (level: number) => void = () => {}): Promise<Recording> {
  if (!navigator.mediaDevices?.getUserMedia || !globalThis.AudioContext) {
    throw new Error('此浏览器无法录音，请使用 HTTPS 或 localhost 并允许麦克风访问')
  }
  const context = new AudioContext({ sampleRate: 16000 })
  let stream: MediaStream | undefined
  let node: AudioWorkletNode | undefined
  let source: MediaStreamAudioSourceNode | undefined
  let chunks: Float32Array[] = []
  let count = 0
  let closed = false
  const cancel = () => {
    closed = true
    stream?.getTracks().forEach(track => track.stop())
    source?.disconnect(); node?.disconnect()
    if (node) node.port.onmessage = null
    chunks = []
    void context.close().catch(() => {})
    signal.removeEventListener('abort', cancel)
  }
  signal.addEventListener('abort', cancel, { once: true })
  try {
    if (signal.aborted) throw new DOMException('Cancelled', 'AbortError')
    await context.resume()
    stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } })
    if (closed) throw new DOMException('Cancelled', 'AbortError')
    await context.audioWorklet.addModule(workletUrl)
    if (closed) throw new DOMException('Cancelled', 'AbortError')
    node = new AudioWorkletNode(context, 'pcm-recorder')
    source = context.createMediaStreamSource(stream)
    source.connect(node)
    // The worklet outputs silence; connecting it keeps processing active.
    node.connect(context.destination)
    const maxSamples = Math.min(context.sampleRate * 120, (8 * 1024 * 1024 - 44) / 2)
    let finish: (() => void) | undefined
    node.port.onmessage = ({ data }) => {
      if (data.done) { finish?.(); return }
      const chunk = (data as Float32Array).slice(0, Math.max(0, maxSamples - count))
      chunks.push(chunk); count += chunk.length
      const rms = Math.sqrt(chunk.reduce((sum, sample) => sum + sample * sample, 0) / Math.max(1, chunk.length))
      onLevel(Math.min(1, rms * 5))
      if (count >= maxSamples && !finish) onLimit()
    }
    return { cancel, stop: async () => {
      if (closed) throw new DOMException('Cancelled', 'AbortError')
      let timeout: ReturnType<typeof setTimeout> | undefined
      try {
        await new Promise<void>((resolve, reject) => {
          finish = resolve
          timeout = setTimeout(() => reject(new Error('录音处理超时，请重新录音')), 2000)
          node!.port.postMessage('stop')
        })
        if (closed) throw new DOMException('Cancelled', 'AbortError')
        if (!count) throw new Error('没有采集到声音，请重新录音')
        return encodeWav(chunks, context.sampleRate)
      } finally { clearTimeout(timeout); cancel() }
    } }
  } catch (error) { cancel(); throw error }
}
