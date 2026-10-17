/**
 * The event viewer's player (EventPlayer.tsx): the camera's recording around one event, with a strip timeline under the
 * video. Pure decisions, kept apart so vitest covers them without a browser: the strip's window, zoom, positions and
 * scale, what is recorded in it, the other events' ticks, previous / next, where each recording chunk starts and what
 * comes after it, when a chunk is stuck (and what to do then), SD or HD, and what to play when the recording is gone.
 *
 * Times are epoch seconds unless a name says otherwise. "Clip time" (chat frames, notes, the Ask tab's `at`) counts from
 * the event's clip_start, the start of its clip.mp4, also when that clip is gone.
 */
import type { NvrEvent, PlaybackQuality, TimelineEvent } from "./api";
import { LIVE_LAG, chunkLen, firstChunkLen, spanAt, type Span } from "./playback";

/** Playback starts this long before the event (Shawn, 2026-10-17: 10 s was too early; the strip has the rest). */
export const PRE_ROLL_S = 2;
/** The server's clip pre-roll (NVR_CLIP_PRE_ROLL default), for an older server whose event detail has no clip_start. */
export const CLIP_PRE_ROLL_S = 5;
/** The strip's zoom levels: this much either side of the event (level 0, the default: ±1 min, so an event of a few
 *  seconds is clearly visible; then ±5 and ±15 min)... */
export const ZOOM_HALF_S = [60, 300, 900] as const;
export const STRIP_HALF_S = ZOOM_HALF_S[0];
/** ...and a long event, plus EVENT_MARGIN_S either side, fills at most this share of it. */
export const STRIP_FILL = 0.6;
export const EVENT_MARGIN_S = 10;
/** A jump to an event (a tick, previous / next, Event start) lands this long before it starts. */
export const JUMP_LEAD_S = 2;
/** Arrow keys on the strip step this far. */
export const KEY_STEP_S = 5;
/** Events this far beyond the strip (or a strip's width, when that is more) are listed too, so previous / next can
 *  leave it. */
export const NEIGHBOUR_RANGE_S = 1800;
/** The last recorded span is still growing when it ends this close to now: chunks aren't cut at its end. */
const GROWING_S = 30;

export type Win = { start: number; end: number };

/** Clip time 0: the event's clip_start, else what the server would have used. */
export const clipOrigin = (e: Pick<NvrEvent, "start_ts" | "clip_start">): number => e.clip_start ?? e.start_ts - CLIP_PRE_ROLL_S;
/** Where playback starts: PRE_ROLL_S before the event (the clip's own longer pre-roll doesn't pull it earlier). */
export const playStart = (e: Pick<NvrEvent, "start_ts" | "clip_start">): number => e.start_ts - PRE_ROLL_S;

/**
 * Offer SD at all? Only when the server converts on a GPU (NVENC and kin): a CPU encoder (libx264) takes ~14 s for a
 * 20 s chunk on our servers, too slow to start playback, so those play HD only (2026-10-17).
 */
export const sdUsable = (c: { sd?: boolean; encoder?: string | null } | null | undefined): boolean =>
  !!c?.sd && /nvenc|qsv|vaapi|videotoolbox|amf|v4l2m2m/i.test(c.encoder ?? "");
/** The event's end; an event still being tracked ends now. */
export const eventEnd = (e: Pick<NvrEvent, "start_ts" | "end_ts">, now: number): number => e.end_ts ?? Math.max(e.start_ts, now);

/**
 * The strip's window at zoom `level` (ZOOM_HALF_S): the event with ZOOM_HALF_S[level] either side, or for a long event
 * wider, so the event and its margins fill STRIP_FILL of it. It doesn't run more than a little past `now` (a recent
 * event is shown with more of what led up to it), but never so far back that the event's end leaves it.
 */
export function stripWindow(start: number, end: number, now: number, level = 0): Win {
  const half = ZOOM_HALF_S[Math.max(0, Math.min(ZOOM_HALF_S.length - 1, level))];
  const e1 = Math.max(end, start);
  const width = Math.max(e1 - start + 2 * half, (e1 - start + 2 * EVENT_MARGIN_S) / STRIP_FILL);
  const mid = (start + e1) / 2;
  let s = mid - width / 2, e = mid + width / 2;
  const over = e - Math.max(now + GROWING_S, e1 + EVENT_MARGIN_S);
  if (over > 0) { s -= over; e -= over; }
  return { start: s, end: e };
}

/**
 * The zoom buttons: the window at `level` around the event when the playhead `t` is in it, else that width centred on
 * the playhead (not running more than a little past `now`), so the playhead stays in view either way.
 */
export function zoomWindow(ev: { start: number; end: number }, level: number, t: number, now: number): Win {
  const w = stripWindow(ev.start, ev.end, now, level);
  if (t >= w.start && t <= w.end) return w;
  const width = w.end - w.start;
  const e = Math.min(t + width / 2, Math.max(now + GROWING_S, t + 0.1 * width));
  return { start: e - width, end: e };
}
/** "±1 min" for a zoom level. */
export const zoomLabel = (level: number): string => `±${Math.round(ZOOM_HALF_S[level] / 60)} min`;

/** What to list (recordings and the camera's events) for window `w`: NEIGHBOUR_RANGE_S or one and a half times the
 *  window's width either side, whichever is more, so previous / next reach past it at any zoom; nothing past now. */
export function listingRange(w: Win, now: number): { from: number; to: number } {
  const pad = Math.max(NEIGHBOUR_RANGE_S, 1.5 * (w.end - w.start));
  return { from: w.start - pad, to: Math.min(w.end + pad, now) };
}
/** List again for window `w`? When nothing is listed yet, or when the listing (`l`) no longer reaches half
 *  NEIGHBOUR_RANGE_S, or the window's width when that is more, beyond either edge (playback moved the window along,
 *  or the strip zoomed out); a listing that reached (nearly) now covers what comes after. */
export function needsListing(l: { from: number; to: number } | undefined, w: Win, now: number): boolean {
  if (!l) return true;
  const pad = Math.max(NEIGHBOUR_RANGE_S / 2, w.end - w.start);
  if (w.start - pad < l.from) return true;
  return w.end + pad > l.to && l.to < now - 60;
}

/** A time label on the strip's scale, `frac` 0..1 across it. */
export type ScaleMark = { t: number; frac: number; label: string };
const SCALE_STEPS_S = [5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200];
/**
 * The labels between the strip's two end labels: round times (every 10 s, 30 s, 1 min, 5 min...) at the smallest step
 * that gives at most `max` of them, none within `edge` (a share of the width) of an end, where the end labels are.
 */
export function scaleMarks(w: Win, max: number, edge = 0.12): ScaleMark[] {
  if (max <= 0) return [];
  const width = w.end - w.start;
  const lo = w.start + edge * width, hi = w.end - edge * width;
  for (const step of SCALE_STEPS_S) {
    const first = Math.ceil(lo / step) * step;
    const n = first > hi ? 0 : Math.floor((hi - first) / step) + 1;
    if (n > max) continue;
    const out: ScaleMark[] = [];
    for (let k = 0; k < n; k++) {
      const t = first + k * step;
      out.push({ t, frac: toFrac(t, w), label: step < 60 ? clock(t) : clock(t).slice(0, 5) });
    }
    return out;
  }
  return [];
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

export type ErrorStep = "retry" | "hd" | "clip" | "still" | "stop";

/**
 * A recording chunk failed to play. `status`: its HTTP status (null = no answer). SD refused for lack of transcode
 * slots (503) on a continuation (`cont`) is asked for again once first: the chunk before it and a dropped prefetch may
 * hold slots for a moment yet. Then, or with no transcoder (501): the same place in HD. After something played: once
 * more (`retry`, unless this was the retry: `retried`), then stop there and say so (`stop`); the strip stays usable.
 * Before anything played (the recording isn't there, 404 or otherwise): the event's own clip, or the snapshot.
 */
export function afterPlaybackError(o: {
  status: number | null; quality: PlaybackQuality; played: boolean; hasClip: boolean; retried?: boolean; cont?: boolean;
}): ErrorStep {
  if (o.quality === "sd" && o.status === 503 && o.cont && !o.retried) return "retry";
  if (o.quality === "sd" && (o.status === 503 || o.status === 501)) return "hd";
  if (o.played) return o.retried ? "stop" : "retry";
  return o.hasClip ? "clip" : "still";
}

/** A chunk whose playhead hasn't moved for this long while it should be playing is stuck... */
export const STUCK_S = 1.5;
/** ...and has run out when that is within this of its end (as asked for, or as far as the browser has it). */
export const NEAR_END_S = 1.5;
/** Stuck mid-way this long (its download died, or the hub tunnel dropped it): ask for it again from there. */
export const MID_STALL_S = 10;
/** A new chunk with no metadata this long after it was asked for is asked for again once, then given up on (a tap
 *  retries). Through the hub the first bytes take longer. */
export const START_TIMEOUT_S = { local: 4, remote: 8 } as const;

/** One look at the playing chunk's <video> (EventPlayer polls it). Seconds. */
export type ChunkLook = {
  /** currentTime; the length asked for; the duration the browser reports (NaN / Infinity: not known); the end of the
   *  buffered range at currentTime */
  ct: number; len: number; duration: number; bufferedEnd: number;
  /** how long currentTime hasn't moved while the viewer wanted it playing */
  idleS: number;
  /** how long ago the chunk was asked for; whether its metadata arrived (readyState >= HAVE_METADATA) */
  sinceLoadS: number; started: boolean;
  wantPlay: boolean; paused: boolean; ended: boolean;
  /** networkState === NETWORK_LOADING: more of the response is coming */
  loading: boolean;
  remote: boolean;
};
export type ChunkStep = "ok" | "advance" | "play" | "reload" | "retry";

/**
 * Has the chunk run out? At (nearly) the length asked for; or at the end of what the browser has when nothing more is
 * coming and the duration it reports ends there too: a streamed transcode can come out a little shorter than its
 * duration says, and then 'ended' may never fire (the video sits on its last frame, waiting).
 */
export function nearChunkEnd(o: Pick<ChunkLook, "ct" | "len" | "duration" | "bufferedEnd" | "loading">): boolean {
  if (o.ct >= o.len - NEAR_END_S) return true;
  if (o.loading || !Number.isFinite(o.duration) || o.duration <= 0) return false;
  return o.duration - o.ct <= NEAR_END_S && o.bufferedEnd - o.ct <= NEAR_END_S;
}

/**
 * What the player does about its chunk now: `advance` to the next chunk as if this one ended (it did, or it is stuck at
 * its end); `play` it again (it is paused but should play: autoplay refused, or the browser paused it); `reload` it
 * from where it is (stuck mid-way for MID_STALL_S); `retry` it (no answer START_TIMEOUT_S after it was asked for).
 * A chunk the viewer paused is left alone.
 */
export function chunkStep(o: ChunkLook): ChunkStep {
  if (!o.started) return o.sinceLoadS >= (o.remote ? START_TIMEOUT_S.remote : START_TIMEOUT_S.local) ? "retry" : "ok";
  if (o.ended) return "advance";
  if (!o.wantPlay || o.idleS < STUCK_S) return "ok";
  if (nearChunkEnd(o)) return "advance";
  if (o.paused) return "play";
  return o.idleS >= MID_STALL_S ? "reload" : "ok";
}

/** Is a 'pause' event the chunk running out rather than a pause someone asked for? Chrome on Android can pause before
 *  'ended' with `ended` still false, and the viewer's wish to play must survive that into the next chunk. */
export const pauseAtEnd = (o: { ct: number; len: number; duration: number; ended: boolean }): boolean =>
  o.ended || o.ct >= o.len - NEAR_END_S || (Number.isFinite(o.duration) && o.duration > 0 && o.ct >= o.duration - 0.25);

/** Prefetch the next chunk this long before the current one ends: half of the current one (a transcode of the next one
 *  needs the time to arrive), at least `minLead`, at most a minute. */
export const prefetchLead = (len: number, minLead: number): number => Math.max(minLead, Math.min(len / 2, 60));

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
