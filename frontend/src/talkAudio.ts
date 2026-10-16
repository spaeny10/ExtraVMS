/**
 * Hold to Talk audio in the browser (talkMic.ts): the microphone's samples, at the AudioContext's rate (48 or 44.1 kHz
 * mostly), down to the speakers' 8 kHz and cut into 40 ms frames of 16-bit little-endian PCM, plus the level meter. The
 * NVR encodes G.711 per speaker (two speakers may take different laws: talkdown.pcm_to_g711), so nothing here knows
 * mu-law. Pure, so it is unit-tested (talkAudio.test.ts).
 */

export const TALK_RATE = 8000;
export const TALK_FRAME_MS = 40;
export const TALK_FRAME_SAMPLES = (TALK_RATE * TALK_FRAME_MS) / 1000;
/** the low-pass cutoff before decimating: telephone band, under 8 kHz's Nyquist with room for the filter's slope */
export const TALK_CUTOFF_HZ = 3400;

/** A windowed-sinc (Blackman) low-pass FIR: `fc` the cutoff as a fraction of the sample rate, `taps` odd; unity gain at DC. */
export function lowpass(fc: number, taps: number): Float32Array {
  const h = new Float32Array(taps);
  const mid = (taps - 1) / 2;
  let sum = 0;
  for (let i = 0; i < taps; i++) {
    const x = i - mid;
    const sinc = x === 0 ? 2 * fc : Math.sin(2 * Math.PI * fc * x) / (Math.PI * x);
    const w = 0.42 - 0.5 * Math.cos((2 * Math.PI * i) / (taps - 1)) + 0.08 * Math.cos((4 * Math.PI * i) / (taps - 1));
    h[i] = sinc * w;
    sum += h[i];
  }
  for (let i = 0; i < taps; i++) h[i] /= sum;
  return h;
}

/**
 * From `inRate` to TALK_RATE, chunk by chunk with its state kept across chunks (the same samples give the same output
 * however they are split): the low-pass, then each output sample interpolated at its fractional input position.
 */
export class Downsampler {
  private readonly h: Float32Array;
  private readonly step: number;
  private hist: Float32Array;     // the last h.length input samples
  private consumed = 0;           // input samples before the current chunk
  private next = 0;               // absolute input position of the next output sample

  constructor(readonly inRate: number, readonly outRate = TALK_RATE) {
    this.step = inRate / outRate;
    // about 16 taps per output sample: at 48 kHz 97 taps, a ~2.7 kHz transition centred on the cutoff
    const taps = inRate > outRate ? 2 * Math.round((this.step * 16) / 2) + 1 : 1;
    this.h = taps > 1 ? lowpass(Math.min(TALK_CUTOFF_HZ, outRate * 0.45) / inRate, taps) : Float32Array.of(1);
    this.hist = new Float32Array(this.h.length);
  }

  push(input: Float32Array): Float32Array {
    const n = this.h.length;
    const x = new Float32Array(n + input.length);
    x.set(this.hist);
    x.set(input, n);
    const base = this.consumed - n;   // x[i] is input sample base + i
    const filtered = (m: number) => {   // the low-passed sample at absolute index m (m >= consumed - 1)
      let acc = 0;
      const top = m - base;
      for (let k = 0; k < n; k++) acc += this.h[k] * x[top - k];
      return acc;
    };
    const out: number[] = [];
    const last = this.consumed + input.length - 1;
    while (this.next < last) {
      const m = Math.floor(this.next);
      const frac = this.next - m;
      out.push(frac ? filtered(m) * (1 - frac) + filtered(m + 1) * frac : filtered(m));
      this.next += this.step;
    }
    this.hist = x.slice(x.length - n);
    this.consumed += input.length;
    return Float32Array.from(out);
  }
}

/** Collects samples and hands them out in frames of `size`. */
export class Framer {
  private buf: Float32Array;
  private n = 0;

  constructor(readonly size = TALK_FRAME_SAMPLES) {
    this.buf = new Float32Array(size);
  }

  push(samples: Float32Array): Float32Array[] {
    const out: Float32Array[] = [];
    for (let i = 0; i < samples.length; i++) {
      this.buf[this.n++] = samples[i];
      if (this.n === this.size) {
        out.push(this.buf);
        this.buf = new Float32Array(this.size);
        this.n = 0;
      }
    }
    return out;
  }
}

/** -1..1 samples as 16-bit little-endian PCM (clipped; 1.0 = 32767), the bytes the NVR takes. */
export function pcm16le(samples: Float32Array): ArrayBuffer {
  const out = new ArrayBuffer(samples.length * 2);
  const v = new DataView(out);
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i])) * 32767;
    v.setInt16(i * 2, Math.trunc(s + (s < 0 ? -0.5 : 0.5)), true);   // rounded half away from zero: symmetric
  }
  return out;
}

/** The meter: a frame's RMS level, -60 dBFS (or quieter) = 0 to 0 dBFS = 1 (a full-scale sine reads 0.95). */
export function levelOf(samples: Float32Array): number {
  if (!samples.length) return 0;
  let sq = 0;
  for (let i = 0; i < samples.length; i++) sq += samples[i] * samples[i];
  const rms = Math.sqrt(sq / samples.length);
  if (rms <= 1e-6) return 0;
  return Math.max(0, Math.min(1, 1 + (20 * Math.log10(rms)) / 60));
}
