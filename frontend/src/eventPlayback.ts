/**
 * The event viewer's player (EventPlayer.tsx): the camera's recording around one event, with a strip timeline under the
 * video. Pure decisions, kept apart so vitest covers them without a browser: the strip's window and positions, what is
 * recorded in it, the other events' ticks, previous / next, where each recording chunk starts and what comes after it,
 * SD or HD, and what to play when the recording is gone.
 *
 * Times are epoch seconds unless a name says otherwise. "Clip time" (chat frames, notes, the Ask tab's `at`) counts from
 * the event's clip_start, the start of its clip.mp4, also when that clip is gone.
 */
import type { NvrEvent, PlaybackQuality, TimelineEvent } from "./api";
import { LIVE_LAG, chunkLen, firstChunkLen, spanAt, type Span } from "./playback";

/** Playback starts this long before the event (or at the clip's start when that is earlier). */
export const PRE_ROLL_S = 10;
/** The server's clip pre-roll (NVR_CLIP_PRE_ROLL default), for an older server whose event detail has no clip_start. */
export const CLIP_PRE_ROLL_S = 5;
/** The strip shows about this much either side of the event... */
export const STRIP_HALF_S = 300;
/** ...and a long event, plus EVENT_MARGIN_S either side, fills this share of it. */
export const STRIP_FILL = 0.6;
export const EVENT_MARGIN_S = 10;
/** A jump to an event (a tick, previous / next, Event start) lands this long before it starts. */
export const JUMP_LEAD_S = 2;
/** Arrow keys on the strip step this far. */
export const KEY_STEP_S = 5;
/** Events this far beyond the strip are listed too, so previous / next can leave it. */
export const NEIGHBOUR_RANGE_S = 1800;
/** The last recorded span is still growing when it ends this close to now: chunks aren't cut at its end. */
const GROWING_S = 30;

export type Win = { start: number; end: number };

/** Clip time 0: the event's clip_start, else what the server would have used. */
export const clipOrigin = (e: Pick<NvrEvent, "start_ts" | "clip_start">): number => e.clip_start ?? e.start_ts - CLIP_PRE_ROLL_S;
/** Where playback starts: PRE_ROLL_S before the event, or the clip's start when that is earlier. */
export const playStart = (e: Pick<NvrEvent, "start_ts" | "clip_start">): number => Math.min(e.start_ts - PRE_ROLL_S, clipOrigin(e));
/** The event's end; an event still being tracked ends now. */
export const eventEnd = (e: Pick<NvrEvent, "start_ts" | "end_ts">, now: number): number => e.end_ts ?? Math.max(e.start_ts, now);

/**
 * The strip's window: ±STRIP_HALF_S around the event, wider for a long event so the event and its margins fill
 * STRIP_FILL of it. It doesn't run more than a little past `now` (a recent event is shown with more of what led up
 * to it), but never so far back that the event's end leaves it.
 */
export function stripWindow(start: number, end: number, now: number): Win {
  const c0 = start - EVENT_MARGIN_S, c1 = Math.max(end, start) + EVENT_MARGIN_S;
  const width = Math.max(2 * STRIP_HALF_S, (c1 - c0) / STRIP_FILL);
  const mid = (c0 + c1) / 2;
  let s = mid - width / 2, e = mid + width / 2;
  const over = e - Math.max(now + GROWING_S, c1);
  if (over > 0) { s -= over; e -= over; }
  return { start: s, end: e };
}

/** The window moved so `t` is inside it again (playback ran past an edge, or a jump left it); same width. */
export function followWindow(w: Win, t: number): Win {
  if (t >= w.start && t <= w.end) return w;
  const width = w.end - w.start;
  const s = t > w.end ? t - 0.2 * width : t - 0.8 * width;
  return { start: s, end: s + width };
}

/** 0..1 across the window (clamped). */
export const toFrac = (t: number, w: Win): number => Math.min(1, Math.max(0, (t - w.start) / (w.end - w.start)));
/** The time at 0..1 across the window (clamped). */
export const atFrac = (f: number, w: Win): number => w.start + Math.min(1, Math.max(0, f)) * (w.end - w.start);

export type Coverage = { start: number; end: number; recorded: boolean };

/** The window cut into recorded and not-recorded pieces, in order, covering all of it. */
export function coverage(spans: Span[], w: Win): Coverage[] {
  const out: Coverage[] = [];
  let t = w.start;
  for (const s of [...spans].sort((a, b) => a.start - b.start)) {
    const a = Math.max(s.start, w.start), b = Math.min(s.end, w.end);
    if (b <= a || b <= t) continue;
    if (a > t) out.push({ start: t, end: a, recorded: false });
    out.push({ start: Math.max(a, t), end: b, recorded: true });
    t = b;
  }
  if (t < w.end) out.push({ start: t, end: w.end, recorded: false });
  return out;
}

/** Does any recording overlap [from, to]? */
export const anyRecorded = (spans: Span[], from: number, to: number): boolean => spans.some((s) => s.end > from && s.start <= to);

export type Tick = { id: number; frac: number; cls: string; title: string };

/** The other events on the camera inside the window, as ticks (the viewer's own event is the highlighted span). */
export function ticks(events: TimelineEvent[], w: Win, currentId: number, label: (e: TimelineEvent) => string): Tick[] {
  return events
    .filter((e) => e.id !== currentId && e.start_ts >= w.start && e.start_ts <= w.end)
    .sort((a, b) => a.start_ts - b.start_ts)
    .map((e) => ({ id: e.id, frac: toFrac(e.start_ts, w), cls: `${e.camera_class} ${e.status}`, title: label(e) }));
}

/** Where a jump to an event lands. */
export const jumpTarget = (e: Pick<TimelineEvent, "start_ts">): number => e.start_ts - JUMP_LEAD_S;

/** "Previous" goes to the event before the one just jumped to while the playhead is less than this past the jump. */
export const PREV_SLACK_S = 4;
/** The event before (dir -1) or after (dir 1) the playhead `t`, by where a jump to it lands; null when there is none.
 *  "Previous" a few seconds after a jump goes to the one before, not to the same one again (PREV_SLACK_S). */
export function neighbour<E extends Pick<TimelineEvent, "start_ts">>(events: E[], t: number, dir: -1 | 1): E | null {
  const sorted = [...events].sort((a, b) => a.start_ts - b.start_ts);
  if (dir < 0) return [...sorted].reverse().find((e) => jumpTarget(e) < t - PREV_SLACK_S) ?? null;
  return sorted.find((e) => jumpTarget(e) > t + 1) ?? null;
}

/** Playback asked for exactly at a recording's listed start can miss it (MediaMTX answers 404: the request's time is
 *  rounded to the millisecond, the segment's isn't), so going to a recording lands this far into it. */
export const EDGE_S = 0.5;
const into = (s: Span) => Math.min(s.start + EDGE_S, (s.start + s.end) / 2);

/** The first recorded moment at or after `from` and before `until`; null when nothing is recorded there. */
export function firstRecorded(spans: Span[], from: number, until: number): number | null {
  if (spanAt(spans, from)) return from;
  const s = [...spans].sort((a, b) => a.start - b.start).find((x) => x.start > from && x.start < until);
  return s ? into(s) : null;
}

/** A tap or drag on the strip at `t`: there if recorded, else the next recording, else the end of the one before;
 *  null when nothing at all is recorded. `dir` -1 (stepping back with the arrow keys) prefers the end of the one before. */
export function snapToRecorded(spans: Span[], t: number, dir: -1 | 1 = 1): number | null {
  if (spanAt(spans, t)) return t;
  const sorted = [...spans].sort((a, b) => a.start - b.start);
  const next = sorted.find((s) => s.start > t);
  const prev = sorted.filter((s) => s.end <= t).pop();
  const before = prev ? Math.max(prev.start, prev.end - 1) : null;
  if (dir < 0 && before != null) return before;
  return next ? into(next) : before;
}

/**
 * The chunk to request at `t`: short first, full length when continuing (playback.ts, `remote` = through the hub),
 * and never past the end of the recorded span it starts in, so a chunk doesn't run across a gap (its video time then
 * stays wall time). The span still growing at the live edge isn't cut. `spans` null = not known: no cut.
 */
export function chunkAt(t: number, spans: Span[] | null, o: { remote: boolean; cont: boolean; now: number }): { start: number; len: number } {
  let len = o.cont ? chunkLen(t, o.remote) : firstChunkLen(t, o.remote);
  const s = spans && spanAt(spans, t);
  if (s && s.end < o.now - GROWING_S) len = Math.min(len, Math.max(1, s.end - t));
  return { start: t, len };
}

/**
 * Where playback continues after a chunk that started at `start` and played `played` seconds: right after it when that
 * is recorded, else at the next recording (a gap is skipped); null at the end of what is recorded, or at the live edge.
 */
export function nextChunkStart(start: number, played: number, spans: Span[] | null, now: number): number | null {
  const next = start + played + 0.1;
  if (next >= now - LIVE_LAG) return null;
  if (!spans) return next;
  if (spanAt(spans, next)) return next;
  const s = [...spans].sort((a, b) => a.start - b.start).find((x) => x.start > next);
  return s ? into(s) : null;
}

/** "/s/<server>/…": the media goes through the hub's tunnel (slow uplink: short chunks); a direct or local URL doesn't. */
export const viaHub = (url: string): boolean => url.startsWith("/s/");

/**
 * The quality to start in: SD on a phone (narrow screen) when the server can transcode, else HD. `canSd` undefined =
 * not known yet: a phone waits for it (null), a wide screen plays HD at once.
 */
export function defaultQuality(narrow: boolean, canSd: boolean | undefined): PlaybackQuality | null {
  if (!narrow) return "hd";
  if (canSd === undefined) return null;
  return canSd ? "sd" : "hd";
}

/** What is on screen: the recording (from `at`), the event's own clip.mp4, or the snapshot (`still`). */
export type Source = { kind: "recording"; at: number } | { kind: "clip" } | { kind: "still" };

/**
 * The first thing to play, once the recordings listing is in (`spans` undefined: still loading, so wait: null;
 * `spans` null: the listing failed, so try the recording anyway). The recording when anything is recorded from
 * `from` to the event's end (`until`); else the event's own clip if it has one; else the snapshot.
 */
export function initialSource(o: { spans: Span[] | null | undefined; from: number; until: number; hasClip: boolean }): Source | null {
  if (o.spans === undefined) return null;
  if (o.spans === null) return { kind: "recording", at: o.from };
  const at = firstRecorded(o.spans, o.from, o.until);
  if (at != null) return { kind: "recording", at };
  return o.hasClip ? { kind: "clip" } : { kind: "still" };
}

export type ErrorStep = "hd" | "clip" | "still" | "stop";

/**
 * A recording chunk failed to play. `status`: its HTTP status (null = no answer). SD refused for lack of transcode slots
 * (503) or a transcoder (501): the same place in HD. Before anything played (the recording isn't there, 404 or
 * otherwise): the event's own clip, or the snapshot. After something played: stop there and say so (`stop`); the strip
 * stays usable.
 */
export function afterPlaybackError(o: { status: number | null; quality: PlaybackQuality; played: boolean; hasClip: boolean }): ErrorStep {
  if (o.quality === "sd" && (o.status === 503 || o.status === 501)) return "hd";
  if (o.played) return "stop";
  return o.hasClip ? "clip" : "still";
}

/** Is any footage of the event there (the Ask tab needs some)? The clip, or a recording overlapping the event.
 *  undefined while the listing loads; a failed listing counts as maybe (the server answers if not). */
export function footageAvailable(o: { spans: Span[] | null | undefined; start: number; end: number; hasClip: boolean }): boolean | undefined {
  if (o.hasClip) return true;
  if (o.spans === undefined) return undefined;
  if (o.spans === null) return true;
  return anyRecorded(o.spans, o.start, o.end);
}

const two = (n: number) => String(n).padStart(2, "0");
/** "14:03:12" (local time). */
export function clock(t: number): string {
  const d = new Date(t * 1000);
  return `${two(d.getHours())}:${two(d.getMinutes())}:${two(d.getSeconds())}`;
}
const span = (s: number) => {
  const r = Math.round(s);
  return r < 60 ? `${r} s` : `${Math.floor(r / 60)}:${two(r % 60)}`;
};
/** Where `t` is relative to the event: "8 s before", "12 s in", "1:05 after". */
export function relLabel(t: number, start: number, end: number): string {
  if (t < start - 0.5) return `${span(start - t)} before`;
  if (t <= end + 0.5) return `${span(Math.max(0, t - start))} in`;
  return `${span(t - end)} after`;
}
