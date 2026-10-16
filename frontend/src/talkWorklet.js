// Hold to Talk's capture (talkMic.ts): an AudioWorklet that hands the microphone's samples to the page in 20 ms blocks.
// A plain same-origin file bundled as an asset, so script-src 'self' allows it on every page (the hub's, the server UI
// under /s/<id>/ whose worker-src is 'none': worklets are scripts, not workers, to CSP). The node's input is mono (the
// graph downmixes: channelCount 1, explicit); the downsampling and encoding happen on the page (talkAudio.ts).
class TalkCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.size = Math.round(sampleRate / 50);
    this.buf = new Float32Array(this.size);
    this.n = 0;
  }

  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    for (let i = 0; i < ch.length; i++) {
      this.buf[this.n++] = ch[i];
      if (this.n === this.size) {
        this.port.postMessage(this.buf, [this.buf.buffer]);
        this.buf = new Float32Array(this.size);
        this.n = 0;
      }
    }
    return true;
  }
}

registerProcessor("talk-capture", TalkCapture);
