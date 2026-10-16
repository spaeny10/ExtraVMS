/**
 * Automatic SD/HD for a live tile: a pure decision over the tile's size and the link-quality samples WhepPlayer's
 * onStats gives every 2 s (whepTiming.statsSample), so vitest covers it with synthetic stats; LiveQuality.tsx feeds it.
 *
 * Up to HD when the tile is big enough to show it (≥ HD_MIN_WIDTH CSS px, or focused: Expand), HD can play in this
 * browser, one of the page's HD slots is free (MAX_HD, dashboard/budget.tsx) and the link has been clean for
 * HEALTHY_MS. Down to SD when the last WINDOW_MS lost too many packets, froze, or dropped too many frames, or when the
 * tile shrank and isn't focused. Anti-flap: at least MIN_GAP_MS between any two switches, and COOLDOWN_MS after a
 * drop for a bad link before trying HD again.
 */
import type { WhepStatsSample } from "./whepTiming";

export type Quality = "sd" | "hd";

export const HD_MIN_WIDTH = 720;
export const HEALTHY_MS = 5_000;
export const WINDOW_MS = 10_000;
export const MIN_GAP_MS = 10_000;
export const COOLDOWN_MS = 30_000;
/** clean enough to go up: every sample of the last HEALTHY_MS under these, no freezes, frames decoding */
export const UP_MAX_LOSS = 0.01;
export const UP_MAX_DROPPED = 0.02;
/** bad enough to go down, over the last WINDOW_MS */
export const DOWN_LOSS = 0.03;
export const DOWN_FREEZES = 2;
export const DOWN_DROPPED = 0.08;
/** samples kept (2 s apart: 20 s, more than either window) */
export const MAX_SAMPLES = 10;

/** One onStats sample and when it arrived (ms, the same clock as `now`). */
export type TimedSample = Pick<WhepStatsSample, "packetsLost" | "packetsReceived" | "freezes" | "framesDropped" | "framesDecoded"> & { at: number };

export type AutoInput = {
  /** the tile's width, CSS px */
  width: number;
  /** the tile is the one expanded (or full screen) */
  focused: boolean;
  hdDecodable: boolean;
  /** false: this browser can't play the SD stream (an H.265 relay); HD, if it plays, is the only picture */
  sdDecodable: boolean;
  /** an HD slot is free for this tile (it holds none yet) */
  hdSlotFree: boolean;
  /** onStats samples of the stream on screen, oldest first */
  samples: TimedSample[];
  now: number;
  current: Quality;
  /** the last switch, and the last drop for a bad link (ms; null: none yet) */
  lastSwitch: number | null;
  lastDrop: number | null;
};

/** A switch to make (`badLink`: a drop for the link's sake, which starts the cooldown), or null to stay. */
export type AutoDecision = { quality: Quality; reason: "big" | "small" | "link" | "sd-unplayable" | "hd-unplayable"; badLink: boolean } | null;

const sum = (xs: TimedSample[], k: keyof Omit<TimedSample, "at">) => xs.reduce((n, s) => n + s[k], 0);

/** Loss, freezes and dropped frames over the samples since `from` (ms). */
export function linkOver(samples: TimedSample[], from: number) {
  const w = samples.filter((s) => s.at > from);
  const lost = sum(w, "packetsLost"), got = sum(w, "packetsReceived");
  const dropped = sum(w, "framesDropped"), decoded = sum(w, "framesDecoded");
  return {
    samples: w.length,
    loss: lost + got ? lost / (lost + got) : 0,
    freezes: sum(w, "freezes"),
    dropped: dropped + decoded ? dropped / (dropped + decoded) : 0,
  };
}

const clean = (s: TimedSample) => {
  const l = linkOver([s], -Infinity);
  return s.framesDecoded > 0 && s.freezes === 0 && l.loss < UP_MAX_LOSS && l.dropped < UP_MAX_DROPPED;
};

/** Clean for the whole of the last HEALTHY_MS: a sample at or before its start (each sample covers the time since the
 *  one before it), and every sample after that clean. */
export function healthyFor(samples: TimedSample[], now: number, ms = HEALTHY_MS): boolean {
  const from = now - ms;
  const recent = samples.filter((s) => s.at > from);
  return recent.length > 0 && samples.some((s) => s.at <= from) && recent.every(clean);
}

export function badLink(samples: TimedSample[], now: number): boolean {
  const l = linkOver(samples, now - WINDOW_MS);
  return l.samples > 0 && (l.loss > DOWN_LOSS || l.freezes >= DOWN_FREEZES || l.dropped > DOWN_DROPPED);
}

export function decideQuality(i: AutoInput): AutoDecision {
  const big = i.width >= HD_MIN_WIDTH || i.focused;
  const gapOk = i.lastSwitch === null || i.now - i.lastSwitch >= MIN_GAP_MS;
  if (i.current === "hd") {
    if (!i.hdDecodable) return i.sdDecodable ? { quality: "sd", reason: "hd-unplayable", badLink: false } : null;
    if (!i.sdDecodable || !gapOk) return null;   // nothing else plays here / too soon after the last switch
    if (badLink(i.samples, i.now)) return { quality: "sd", reason: "link", badLink: true };
    if (!big) return { quality: "sd", reason: "small", badLink: false };
    return null;
  }
  if (!i.hdDecodable) return null;
  if (!i.sdDecodable) return { quality: "hd", reason: "sd-unplayable", badLink: false };   // the only picture there is
  if (!big || !i.hdSlotFree || !gapOk) return null;
  if (i.lastDrop !== null && i.now - i.lastDrop < COOLDOWN_MS) return null;
  return healthyFor(i.samples, i.now) ? { quality: "hd", reason: "big", badLink: false } : null;
}

/** Add a sample, keeping the newest MAX_SAMPLES. */
export const pushSample = (samples: TimedSample[], s: WhepStatsSample, at: number): TimedSample[] =>
  [...samples, { at, packetsLost: s.packetsLost, packetsReceived: s.packetsReceived, freezes: s.freezes, framesDropped: s.framesDropped,
    framesDecoded: s.framesDecoded }].slice(-MAX_SAMPLES);
