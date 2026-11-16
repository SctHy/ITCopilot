// Transfer batches, not an unbounded recording, to the main thread.
class PcmRecorder extends AudioWorkletProcessor {
  constructor() {
    super()
    this.buffer = new Float32Array(2048)
    this.offset = 0
    this.done = false
    this.port.onmessage = () => {
      this.flush()
      this.done = true
      this.port.postMessage({ done: true })
    }
  }
  flush() {
    if (this.offset) this.port.postMessage(this.buffer.slice(0, this.offset))
    this.offset = 0
  }
  process(inputs) {
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
