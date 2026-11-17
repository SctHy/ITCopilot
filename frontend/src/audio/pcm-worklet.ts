// These globals exist in the worklet, outside the DOM type library.
declare abstract class AudioWorkletProcessor {
  readonly port: MessagePort
  abstract process(inputs: Float32Array[][]): boolean
}
declare function registerProcessor(name: string, processor: new () => AudioWorkletProcessor): void

// Transfer batches, not an unbounded recording, to the main thread.
class PcmRecorder extends AudioWorkletProcessor {
  private readonly buffer = new Float32Array(2048)
  private offset = 0
  private done = false

  constructor() {
    super()
    this.port.onmessage = () => {
      this.flush()
      this.done = true
      this.port.postMessage({ done: true })
    }
  }
  private flush() {
    if (this.offset) this.port.postMessage(this.buffer.slice(0, this.offset))
    this.offset = 0
  }
  process(inputs: Float32Array[][]) {
    if (this.done) return false
    const input = inputs[0]?.[0]
    if (input) for (const sample of input) {
      this.buffer[this.offset++] = sample
      if (this.offset === this.buffer.length) this.flush()
    }
    return true
  }
}
registerProcessor('pcm-recorder', PcmRecorder)
