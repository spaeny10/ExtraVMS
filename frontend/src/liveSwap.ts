/**
 * Seamless SD/HD switch for LivePlayer: the stream on screen keeps playing while the next path connects out of sight,
 * and the two swap when the next one shows its first frame; then the old one unmounts (WhepPlayer sends its WHEP
 * DELETE). If the next one fails (unsupported, falls back, or no frame within SWAP_TIMEOUT_MS) it is dropped and the
 * old path stays: `failed` names it until the caller asks for another path. Pure, so vitest covers the sequencing.
 *
 * A stream on screen that never showed a frame (still connecting, or its recording fallback) has nothing worth
 * keeping: a new path replaces it at once, as the old remount did.
 */

export const SWAP_TIMEOUT_MS = 10_000;

/** One mounted player: `id` is its React key (stable from connecting hidden to on screen, so it never reconnects);
 *  `swapIn`: it started hidden (it never shows the connecting still: the old picture covers that time). */
export type SwapStream = { id: number; path: string; framed: boolean; since: number; hidden: boolean; swapIn: boolean };
export type SwapState = { active: SwapStream; next: SwapStream | null; failed: string | null; seq: number };

export type SwapEvent =
  /** the caller wants this path on screen */
  | { type: "path"; path: string; now: number }
  /** a stream presented (or decoded) its first video frame */
  | { type: "frame"; id: number }
  /** a stream gave up: the browser can't decode it, or WebRTC failed and it would fall back */
  | { type: "fail"; id: number }
  /** time passing: a next stream without a frame for SWAP_TIMEOUT_MS has failed */
  | { type: "tick"; now: number };

export function swapInit(path: string, now: number): SwapState {
  return { active: { id: 1, path, framed: false, since: now, hidden: false, swapIn: false }, next: null, failed: null, seq: 1 };
}

export function swapReducer(s: SwapState, e: SwapEvent): SwapState {
  switch (e.type) {
    case "path": {
      if (e.path === s.active.path) return s.next || s.failed ? { ...s, next: null, failed: null } : s;   // back to what plays
      if (e.path === s.next?.path || e.path === s.failed) return s;
      const seq = s.seq + 1;
      if (!s.active.framed) {
        return { active: { id: seq, path: e.path, framed: false, since: e.now, hidden: false, swapIn: false }, next: null, failed: null, seq };
      }
      return { ...s, next: { id: seq, path: e.path, framed: false, since: e.now, hidden: true, swapIn: true }, failed: null, seq };
    }
    case "frame":
      if (s.next?.id === e.id) return { ...s, active: { ...s.next, framed: true, hidden: false }, next: null };
      if (s.active.id === e.id && !s.active.framed) return { ...s, active: { ...s.active, framed: true } };
      return s;
    case "fail":
      return s.next?.id === e.id ? { ...s, next: null, failed: s.next.path } : s;
    case "tick":
      return s.next && e.now - s.next.since >= SWAP_TIMEOUT_MS ? { ...s, next: null, failed: s.next.path } : s;
  }
}

/** The players to mount, in order: the one on screen, then the next one (hidden) while it connects. */
export const swapStreams = (s: SwapState): SwapStream[] => (s.next ? [s.active, s.next] : [s.active]);
