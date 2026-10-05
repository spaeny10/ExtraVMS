import { describe, expect, it } from "vitest";
import { LEASE_KEY, LEASE_MS, MUTE_KEY, Ringer, leaseDecision, muteActive, parseLease, patternFor } from "./ringer";

describe("patternFor", () => {
  it("is more insistent the higher the priority", () => {
    const h = patternFor("high"), m = patternFor("medium"), l = patternFor("low");
    expect(h.repeatS).toBeLessThan(m.repeatS);
    expect(m.repeatS).toBeLessThan(l.repeatS);
    expect(h.tones.length).toBeGreaterThan(m.tones.length);
    expect(m.tones.length).toBeGreaterThan(l.tones.length);
  });
});

describe("leaseDecision", () => {
  it("takes a free, expired or own lease", () => {
    expect(leaseDecision(null, "a", true, 100)).toEqual({ own: true, write: { tab: "a", until: 100 + LEASE_MS }, release: false });
    expect(leaseDecision({ tab: "b", until: 99 }, "a", true, 100).own).toBe(true);
    expect(leaseDecision({ tab: "a", until: 5000 }, "a", true, 100).write).toEqual({ tab: "a", until: 100 + LEASE_MS });
  });
  it("leaves another live tab's lease alone", () => {
    expect(leaseDecision({ tab: "b", until: 5000 }, "a", true, 100)).toEqual({ own: false, write: null, release: false });
    expect(leaseDecision({ tab: "b", until: 5000 }, "a", false, 100)).toEqual({ own: false, write: null, release: false });
  });
  it("gives up its own lease when it stops wanting to ring", () => {
    expect(leaseDecision({ tab: "a", until: 5000 }, "a", false, 100)).toEqual({ own: false, write: null, release: true });
  });
  it("parses defensively", () => {
    expect(parseLease("{bad")).toBe(null);
    expect(parseLease(JSON.stringify({ tab: 1 }))).toBe(null);
    expect(parseLease(JSON.stringify({ tab: "a", until: 3 }))).toEqual({ tab: "a", until: 3 });
  });
});

it("mute lapses on its own", () => {
  expect(muteActive(null, 1)).toBe(false);
  expect(muteActive(10, 5)).toBe(true);
  expect(muteActive(10, 10)).toBe(false);
});

/** Two tabs sharing one storage: only one rings, the other takes over when the first stops. */
describe("Ringer across tabs", () => {
  const mem = () => { const m = new Map<string, string>(); return { getItem: (k: string) => m.get(k) ?? null, setItem: (k: string, v: string) => void m.set(k, v), removeItem: (k: string) => void m.delete(k) }; };
  const fakeAudio = () => ({ state: "running", resume: async () => {}, currentTime: 0, destination: {}, createOscillator: () => ({ frequency: {}, connect: (g: unknown) => g, start: () => {}, stop: () => {} }),
    createGain: () => ({ gain: { setValueAtTime: () => {}, linearRampToValueAtTime: () => {} }, connect: (d: unknown) => d }) }) as unknown as AudioContext;
  it("one tab rings, mute is shared, the lease passes on", async () => {
    const store = mem();
    let now = 1000;
    const deps = { storage: () => store, channel: () => null, audio: fakeAudio, now: () => now };
    const a = new Ringer(deps), b = new Ringer(deps);
    await a.enable(); await b.enable();
    a.want("high"); b.want("high");
    expect(a.get().ringingHere).toBe(true);
    expect(b.get().ringingHere).toBe(false);
    expect(b.get().ringingElsewhere).toBe(true);
    b.mute();
    expect(store.getItem(MUTE_KEY)).not.toBe(null);
    a.want("high");
    expect(a.get().ringingHere).toBe(false);
    a.unmute();
    a.want(null);
    expect(store.getItem(LEASE_KEY)).toBe(null);
    now += 10;
    b.want("high");
    expect(b.get().ringingHere).toBe(true);
    a.stop(); b.stop();
  });
  it("no gesture yet: never takes the lease", () => {
    const store = mem();
    const r = new Ringer({ storage: () => store, channel: () => null, audio: () => null, now: () => 1 });
    r.want("high");
    expect(r.get().needsGesture).toBe(true);
    expect(store.getItem(LEASE_KEY)).toBe(null);
    r.stop();
  });
});
