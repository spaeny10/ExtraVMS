import { describe, expect, it } from "vitest";
import { Downsampler, Framer, TALK_FRAME_SAMPLES, levelOf, lowpass, pcm16le } from "./talkAudio";

const sine = (hz: number, rate: number, n: number, amp = 1, from = 0) =>
  Float32Array.from({ length: n }, (_, i) => amp * Math.sin((2 * Math.PI * hz * (i + from)) / rate));
const rms = (x: Float32Array) => Math.sqrt(x.reduce((a, v) => a + v * v, 0) / x.length);
const concat = (parts: Float32Array[]) => {
  const out = new Float32Array(parts.reduce((a, p) => a + p.length, 0));
  let o = 0;
  for (const p of parts) { out.set(p, o); o += p.length; }
  return out;
};

describe("the microphone down to the speakers' 8 kHz", () => {
  it("the low-pass passes DC unchanged and is symmetric", () => {
    const h = lowpass(3400 / 48000, 97);
    expect(h.reduce((a, v) => a + v, 0)).toBeCloseTo(1, 6);
    expect(h[0]).toBeCloseTo(h[96], 9);
    expect(Math.max(...h)).toBe(h[48]);
  });

  it("one 8 kHz sample per 6 at 48 kHz, per 5.5125 at 44.1 kHz, however the input is split", () => {
    const d = new Downsampler(48000);
    expect(d.push(new Float32Array(4800)).length).toBe(800);
    expect(d.push(new Float32Array(4801)).length).toBe(800);   // the 801st (at 9600) waits for its neighbour
    expect(d.push(new Float32Array(1)).length).toBe(1);
    const e = new Downsampler(44100);
    const n = [1000, 37, 4410, 128, 5].reduce((a, len) => a + e.push(new Float32Array(len)).length, 0);
    expect(Math.abs(n - (5580 * 8000) / 44100)).toBeLessThanOrEqual(1);
    // chunking never changes the output: 20 ms worklet blocks, 1024-sample script processor blocks or one piece
    const x = sine(440, 48000, 9600, 0.5);
    const whole = new Downsampler(48000).push(x);
    const split = new Downsampler(48000);
    const parts: Float32Array[] = [];
    for (let i = 0; i < x.length; i += 1024) parts.push(split.push(x.subarray(i, i + 1024)));
    expect(Array.from(concat(parts))).toEqual(Array.from(whole));
  });

  it("speech band passes at full level, above 4 kHz is filtered out (no aliasing)", () => {
    const pass = new Downsampler(48000).push(sine(1000, 48000, 48000, 0.8)).subarray(400);   // after the filter has filled
    expect(rms(pass)).toBeGreaterThan(0.8 * Math.SQRT1_2 * 0.97);
    expect(rms(pass)).toBeLessThan(0.8 * Math.SQRT1_2 * 1.03);
    // the 1 kHz tone is still 1 kHz: 8 samples a period, so sign changes every 4 samples
    const crossings = pass.slice(1).filter((v, i) => Math.sign(v) !== Math.sign(pass[i])).length;
    expect(Math.abs(crossings - (2 * 1000 * pass.length) / 8000)).toBeLessThanOrEqual(2);
    const stop = new Downsampler(48000).push(sine(6000, 48000, 48000, 0.8)).subarray(400);   // would alias to 2 kHz
    expect(rms(stop)).toBeLessThan(0.8 * Math.SQRT1_2 * 0.03);   // at least 30 dB down
    const dc = new Downsampler(44100).push(new Float32Array(44100).fill(0.25)).subarray(200);
    expect(Math.max(...dc.map((v) => Math.abs(v - 0.25)))).toBeLessThan(1e-4);
  });

  it("an 8 kHz input passes through unfiltered", () => {
    const x = Float32Array.from([0, 0.5, -0.5, 1, -1]);
    expect(Array.from(new Downsampler(8000).push(x))).toEqual([0, 0.5, -0.5, 1]);
  });
});

describe("frames and PCM for the NVR", () => {
  it("cuts samples into 40 ms frames of 320, keeping the rest for the next push", () => {
    const f = new Framer();
    expect(TALK_FRAME_SAMPLES).toBe(320);
    expect(f.push(new Float32Array(300))).toEqual([]);
    const out = f.push(Float32Array.from({ length: 400 }, (_, i) => i + 1));
    expect(out.length).toBe(2);
    expect([out[0][299], out[0][300], out[1][0], out[1][59]]).toEqual([0, 1, 21, 80]);   // in order across pushes
    expect(f.push(new Float32Array(259)).length).toBe(0);   // 60 + 259: one short
    expect(f.push(new Float32Array(1)).length).toBe(1);
  });

  it("16-bit little-endian, full scale 32767, clipped", () => {
    const b = new DataView(pcm16le(Float32Array.from([0, 1, -1, 0.5, -0.5, 2, -2, 0x1234 / 32767])));
    expect([0, 1, 2, 3, 4, 5, 6].map((i) => b.getInt16(i * 2, true))).toEqual([0, 32767, -32767, 16384, -16384, 32767, -32767]);
    expect(new Uint8Array(b.buffer, 14, 2)).toEqual(Uint8Array.from([0x34, 0x12]));   // low byte first
  });

  it("the level meter follows the real signal in dBFS", () => {
    expect(levelOf(new Float32Array(320))).toBe(0);
    expect(levelOf(sine(1000, 8000, 320))).toBeCloseTo(1 + (20 * Math.log10(Math.SQRT1_2)) / 60, 3);   // full-scale sine: -3 dB
    expect(levelOf(new Float32Array(320).fill(10 ** (-30 / 20)))).toBeCloseTo(0.5, 3);   // -30 dBFS: half way
    expect(levelOf(new Float32Array(320).fill(1e-4))).toBe(0);   // under -60 dBFS
  });
});
