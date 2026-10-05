/**
 * The alarm sound. It plays while any ringing-lane incident is unclaimed (the page tells it the most urgent priority,
 * queue.ringPriority) and stops the moment that is no longer true, i.e. on claim.
 *
 * One tab rings: an operator with the console open in three tabs must not hear three alarms out of step. Tabs that
 * are able to play (sound enabled by a gesture) compete for a lease in localStorage (`socRinger.lease`, renewed every
 * second, 4 s life, so a closed or frozen tab loses it quickly); a BroadcastChannel tells the others at once when the
 * owner lets go, so the hand-over doesn't wait for the lease to run out. Mute is shared the same way (one `m` silences
 * every tab) and lifts itself after 15 minutes, so a muted console can't stay silent through the night.
 *
 * Browsers only start audio after a user gesture: until then `needsGesture` is true and the header shows
 * "Enable sound". The pure parts (patternFor, leaseDecision, muteActive) are unit-tested in ringer.test.ts.
 */
import type { Priority } from "./types";

export type Tone = { freq: number; ms: number; gapMs: number; gain: number };
export type Pattern = { tones: Tone[]; repeatS: number };

/** High: fast falling triple, every 2 s. Medium: double, every 4 s. Low: one soft tone, every 8 s. */
export function patternFor(p: Priority): Pattern {
  if (p === "high") return { repeatS: 2, tones: [{ freq: 988, ms: 160, gapMs: 70, gain: 0.32 }, { freq: 784, ms: 160, gapMs: 70, gain: 0.32 }, { freq: 988, ms: 220, gapMs: 0, gain: 0.32 }] };
  if (p === "medium") return { repeatS: 4, tones: [{ freq: 740, ms: 200, gapMs: 120, gain: 0.25 }, { freq: 740, ms: 200, gapMs: 0, gain: 0.25 }] };
  return { repeatS: 8, tones: [{ freq: 587, ms: 260, gapMs: 0, gain: 0.16 }] };
}

/** The escalation chime (an incident reached level 2: supervisors paged): rising, unlike any ring pattern. */
export const ESCALATION_CHIME: Tone[] = [
  { freq: 523, ms: 140, gapMs: 40, gain: 0.3 }, { freq: 659, ms: 140, gapMs: 40, gain: 0.3 }, { freq: 784, ms: 140, gapMs: 40, gain: 0.3 }, { freq: 1047, ms: 320, gapMs: 0, gain: 0.3 },
];

export const LEASE_KEY = "socRinger.lease";
export const MUTE_KEY = "socRinger.mute";
export const LEASE_MS = 4000;
export const MUTE_MS = 15 * 60 * 1000;

export type Lease = { tab: string; until: number };

export function parseLease(raw: string | null): Lease | null {
  try {
    const v = JSON.parse(raw ?? "null");
    return v && typeof v.tab === "string" && typeof v.until === "number" ? v : null;
  } catch { return null; }
}

/**
 * Who rings. A tab that wants to ring takes a free, expired or own lease (and renews it); otherwise another live tab
 * owns it. A tab that doesn't want to ring gives up a lease it holds (`release`), leaving others' alone.
 */
export function leaseDecision(cur: Lease | null, tab: string, wants: boolean, now: number, ms = LEASE_MS): { own: boolean; write: Lease | null; release: boolean } {
  const mine = cur?.tab === tab;
  if (!wants) return { own: false, write: null, release: mine };
  if (!cur || mine || cur.until <= now) return { own: true, write: { tab, until: now + ms }, release: false };
  return { own: false, write: null, release: false };
}

/** Mute until a time (epoch ms); past it the mute no longer counts. */
export const muteActive = (until: number | null, now: number) => until != null && until > now;

// ---- the browser side

type KV = Pick<Storage, "getItem" | "setItem" | "removeItem">;
export type RingerState = { enabled: boolean; needsGesture: boolean; mutedUntil: number | null; wanted: Priority | null; ringingHere: boolean; ringingElsewhere: boolean };

type Deps = { storage: () => KV | null; channel: () => BroadcastChannel | null; audio: () => AudioContext | null; now: () => number };

const browserDeps: Deps = {
  storage: () => { try { return localStorage; } catch { return null; } },
  channel: () => { try { return typeof BroadcastChannel === "function" ? new BroadcastChannel("socRinger") : null; } catch { return null; } },
  audio: () => {
    const C = (globalThis as { AudioContext?: typeof AudioContext; webkitAudioContext?: typeof AudioContext }).AudioContext
      ?? (globalThis as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    try { return C ? new C() : null; } catch { return null; }
  },
  now: () => Date.now(),
};

export class Ringer {
  readonly tab = Math.random().toString(36).slice(2, 10);
  private ctx: AudioContext | null = null;
  private chan: BroadcastChannel | null = null;
  private timer: ReturnType<typeof setInterval> | null = null;
  private lastPlay = 0;
  private repeatS: number | null = null;
  private listeners = new Set<() => void>();
  private state: RingerState = { enabled: false, needsGesture: true, mutedUntil: null, wanted: null, ringingHere: false, ringingElsewhere: false };

  constructor(private deps: Deps = browserDeps) {
    this.chan = deps.channel();
    if (this.chan) this.chan.onmessage = () => this.tick();
    try { const m = Number(deps.storage()?.getItem(MUTE_KEY)); this.state.mutedUntil = m > 0 ? m : null; } catch { /* no storage */ }
  }

  get = () => this.state;
  subscribe = (fn: () => void) => { this.listeners.add(fn); return () => { this.listeners.delete(fn); }; };
  private set(p: Partial<RingerState>) {
    const next = { ...this.state, ...p };
    if (Object.keys(p).every((k) => next[k as keyof RingerState] === this.state[k as keyof RingerState])) return;
    this.state = next;
    this.listeners.forEach((f) => f());
  }

  /** Call from a click/keydown handler: creates or resumes the AudioContext (browsers refuse without a gesture). */
  enable = async () => {
    if (!this.ctx) this.ctx = this.deps.audio();
    if (!this.ctx) return;
    try { if (this.ctx.state === "suspended") await this.ctx.resume(); } catch { /* still blocked */ }
    const ok = this.ctx.state === "running";
    this.set({ enabled: ok, needsGesture: !ok });
    this.tick();
  };

  /** The most urgent unclaimed ringing priority (null = silence); `repeatS` = the hub's repeat interval, a ceiling. */
  want(p: Priority | null, repeatS?: number | null) {
    this.repeatS = repeatS ?? null;
    if (p !== this.state.wanted) { this.lastPlay = 0; this.set({ wanted: p }); }
    this.start();
    this.tick();
  }

  mute(ms = MUTE_MS) { this.writeMute(this.deps.now() + ms); }
  unmute() { this.writeMute(null); }
  toggleMute() { if (muteActive(this.state.mutedUntil, this.deps.now())) this.unmute(); else this.mute(); }
  private writeMute(until: number | null) {
    try { const s = this.deps.storage(); if (until) s?.setItem(MUTE_KEY, String(until)); else s?.removeItem(MUTE_KEY); } catch { /* no storage: this tab only */ }
    this.set({ mutedUntil: until });
    this.chan?.postMessage({ mute: until });
    this.tick();
  }

  /** The escalation chime, in whichever tab owns the lease (or this one when no tab is ringing). Ignores mute: L2 matters. */
  chime() {
    const s = this.deps.storage();
    const cur = parseLease(s?.getItem(LEASE_KEY) ?? null);
    if (cur && cur.tab !== this.tab && cur.until > this.deps.now()) return;
    this.play(ESCALATION_CHIME);
  }

  /**
   * The chime once per incident (escalation frames repeat the level; the supervisor view also chimes for incidents
   * that were already escalated when it opened). Returns false when this incident has had its chime.
   */
  private chimed = new Set<number>();
  chimeOnce(id: number): boolean {
    if (this.chimed.has(id)) return false;
    this.chimed.add(id);
    this.chime();
    return true;
  }
  /** ids that have had their chime (read by supervisor.chimeDue) */
  get chimedIds(): ReadonlySet<number> { return this.chimed; }

  private start() {
    if (this.timer) return;
    this.timer = setInterval(() => this.tick(), 1000);
  }

  /** Silence and give up the lease (the console is closing). */
  stop() {
    this.want(null);
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }

  private tick() {
    const now = this.deps.now();
    const s = this.deps.storage();
    // re-read mute (another tab may have muted or unmuted), and let an expired mute lapse
    let mutedUntil = this.state.mutedUntil;
    try { const m = Number(s?.getItem(MUTE_KEY)); mutedUntil = m > 0 ? m : null; } catch { /* keep ours */ }
    if (mutedUntil && !muteActive(mutedUntil, now)) { mutedUntil = null; try { s?.removeItem(MUTE_KEY); } catch { /* */ } }
    const wants = !!this.state.wanted && this.state.enabled && !muteActive(mutedUntil, now);
    const cur = parseLease(s?.getItem(LEASE_KEY) ?? null);
    const d = leaseDecision(cur, this.tab, wants, now);
    try {
      if (d.write) s?.setItem(LEASE_KEY, JSON.stringify(d.write));
      if (d.release) { s?.removeItem(LEASE_KEY); this.chan?.postMessage({ released: this.tab }); }
    } catch { /* no storage: every tab rings, better than none */ }
    const own = d.own || (wants && !s);
    const elsewhere = !own && !!this.state.wanted && !!cur && cur.tab !== this.tab && cur.until > now;
    this.set({ mutedUntil, ringingHere: own, ringingElsewhere: elsewhere });
    if (own && this.state.wanted) {
      const p = patternFor(this.state.wanted);
      const every = Math.min(this.repeatS ?? p.repeatS, p.repeatS) * 1000;
      if (now - this.lastPlay >= every) { this.lastPlay = now; this.play(p.tones); }
    }
    if (!this.state.wanted && this.timer && !d.release) { clearInterval(this.timer); this.timer = null; }
  }

  private play(tones: Tone[]) {
    const ctx = this.ctx;
    if (!ctx || ctx.state !== "running") return;
    let t = ctx.currentTime + 0.02;
    for (const tone of tones) {
      const osc = ctx.createOscillator();
      const g = ctx.createGain();
      osc.type = "square";
      osc.frequency.value = tone.freq;
      // short attack/release envelope: square waves click without one
      g.gain.setValueAtTime(0, t);
      g.gain.linearRampToValueAtTime(tone.gain, t + 0.01);
      g.gain.setValueAtTime(tone.gain, t + tone.ms / 1000 - 0.02);
      g.gain.linearRampToValueAtTime(0, t + tone.ms / 1000);
      osc.connect(g).connect(ctx.destination);
      osc.start(t);
      osc.stop(t + tone.ms / 1000 + 0.01);
      t += (tone.ms + tone.gapMs) / 1000;
    }
  }
}

let shared: Ringer | null = null;
/** The page's one ringer (module-level so the header toggle and the stream share it). */
export const ringer = () => (shared ??= new Ringer());
