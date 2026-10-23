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
import { clampRange, type RangeLimits } from "./timeZoom";

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
/** Wheel / pinch zoom limits for the strip's width: the Timeline page's shortest window (1 min), and up to 2 h, or
 *  wider for a long event, so the whole event (and its margins) is always within reach of a zoom out. */
export const MIN_STRIP_S = 60;
export const MAX_STRIP_S = 7200;
/** How far past now the strip may be panned or zoomed: this share of its width (like the Timeline page), at least GROWING_S. */
const FUTURE_SHARE = 0.15;
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
  return eventWindow(start, end, now, stripWidth(start, end, level));
}
/** The strip's width at zoom `level` for an event from `start` to `end` (see stripWindow). */
export function stripWidth(start: number, end: number, level = 0): number {
  const half = ZOOM_HALF_S[Math.max(0, Math.min(ZOOM_HALF_S.length - 1, level))];
  const len = Math.max(end, start) - start;
  return Math.max(len + 2 * half, (len + 2 * EVENT_MARGIN_S) / STRIP_FILL);
}
/** A window `width` wide centred on the event, not running more than a little past `now` (but never so far back that
 *  the event's end leaves it). */
export function eventWindow(start: number, end: number, now: number, width: number): Win {
  const e1 = Math.max(end, start);
  const mid = (start + e1) / 2;
  let s = mid - width / 2, e = mid + width / 2;
  const over = e - Math.max(now + GROWING_S, e1 + EVENT_MARGIN_S);
  if (over > 0) { s -= over; e -= over; }
  return { start: s, end: e };
}
/** Wheel / pinch limits for an event's strip: MIN_STRIP_S to MAX_STRIP_S, or the widest zoom level when that is wider. */
export const stripLimits = (ev: { start: number; end: number }): RangeLimits =>
  ({ min: MIN_STRIP_S, max: Math.max(MAX_STRIP_S, stripWidth(ev.start, ev.end, ZOOM_HALF_S.length - 1)) });
/** A window after a wheel, pinch or drag: its width within `lim` (around its centre), and not more than FUTURE_SHARE of
 *  it (or GROWING_S) past now. */
export function clampStrip(w: Win, now: number, lim: RangeLimits): Win {
  const width = clampRange(w.end - w.start, lim);
  const mid = (w.start + w.end) / 2;
  let s = mid - width / 2, e = mid + width / 2;
  const maxEnd = now + Math.max(GROWING_S, FUTURE_SHARE * width);
  if (e > maxEnd) { s -= e - maxEnd; e = maxEnd; }
  return { start: s, end: e };
}

/**
 * The zoom buttons: the window at `level` around the event when the playhead `t` is in it, else that width centred on
 * the playhead (not running more than a little past `now`), so the playhead stays in view either way.
 */
export function zoomWindow(ev: { start: number; end: number }, level: number, t: number, now: number): Win {
  return zoomWindowWidth(ev, stripWidth(ev.start, ev.end, level), t, now);
}
/** zoomWindow for any width (the buttons step to the zoom levels' widths, and to the wheel limits past them). */
export function zoomWindowWidth(ev: { start: number; end: number }, width: number, t: number, now: number): Win {
  const w = eventWindow(ev.start, ev.end, now, width);
  if (t >= w.start && t <= w.end) return w;
  const e = Math.min(t + width / 2, Math.max(now + GROWING_S, t + 0.1 * width));
  return { start: e - width, end: e };
}
/** "±1 min" for a zoom level. */
export const zoomLabel = (level: number): string => halfLabel(ZOOM_HALF_S[level]);
/** "±30 s", "±1 min", "±2.5 min", "±15 min", "±1 h": half a strip's width, rounded to what reads at a glance. */
export function halfLabel(half: number): string {
  const tidy = (x: number) => String(Math.round(x * 2) / 2);
  if (half < 57) return `±${Math.max(5, Math.round(half / 5) * 5)} s`;
  if (half < 600) return `±${tidy(half / 60)} min`;
  if (half < 3570) return `±${Math.round(half / 60)} min`;
  return `±${tidy(half / 3600)} h`;
}
/**
 * The −/+ buttons from a strip `width` wide: the next stop out (dir 1) or in (dir -1) that differs from it by more than
 * 5%: the zoom levels' widths for this event, and the wheel limits past them; null when already there (disabled).
 */
export function zoomStep(ev: { start: number; end: number }, width: number, dir: -1 | 1): number | null {
  const lim = stripLimits(ev);
  const stops = [...new Set([lim.min, ...ZOOM_HALF_S.map((_, i) => stripWidth(ev.start, ev.end, i)), lim.max])]
    .filter((x) => x >= lim.min && x <= lim.max).sort((a, b) => a - b);
  const pick = dir > 0 ? stops.find((x) => x > width * 1.05) : [...stops].reverse().find((x) => x < width / 1.05);
  return pick ?? null;
}

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

/** The window moved so `t` is inside it again (playback ran past an edge, or a jump left it); same width. `prev`: the
 *  playhead before (playback moving on): when that was already outside, the viewer panned or zoomed away to look
 *  elsewhere, and the window stays there. */
export function followWindow(w: Win, t: number, prev?: number): Win {
  if (t >= w.start && t <= w.end) return w;
  if (prev != null && (prev < w.start || prev > w.end)) return w;
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

/** `frac` where it starts, `len` its length as a share of the window (0 for an event still open: drawn a few px wide). */
export type Tick = { id: number; frac: number; len: number; cls: string; title: string };

/** The other events on the camera inside the window, as ticks (the viewer's own event is the highlighted span). */
export function ticks(events: TimelineEvent[], w: Win, currentId: number, label: (e: TimelineEvent) => string): Tick[] {
  return events
    .filter((e) => e.id !== currentId && e.start_ts >= w.start && e.start_ts <= w.end)
    .sort((a, b) => a.start_ts - b.start_ts)
    .map((e) => {
      const frac = toFrac(e.start_ts, w);
      return { id: e.id, frac, len: e.end_ts == null ? 0 : Math.max(0, toFrac(e.end_ts, w) - frac), cls: `${e.camera_class} ${e.status}`, title: label(e) };
    });
}

/** The camera's events the strip shows and previous / next step through: everything listed but masked ones (a mask,
 *  or hidden by hand). */
export const listedEvents = <E extends Pick<TimelineEvent, "status">>(events: E[]): E[] => events.filter((x) => x.status !== "masked");
/** ...and the selected event among them, also when the listing doesn't have it (yet, or masked). */
export function withSelected<E extends Pick<TimelineEvent, "id">>(events: E[], selected: E): E[] {
  return events.some((x) => x.id === selected.id) ? events : [...events, selected];
}
/** The strip after switching the viewer to the event from `start` to `end`: the same width (the zoom is kept), centred
 *  on it, or for an event too long for it, its start a fifth of the way in; not more than a little past now. */
export function centreOn(w: Win, start: number, end: number, now: number): Win {
  const width = w.end - w.start;
  const len = Math.max(end, start) - start;
  if (len + 2 * EVENT_MARGIN_S <= width) return eventWindow(start, end, now, width);
  const s = start - 0.2 * width;
  return { start: s, end: s + width };
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

/**
 * Previous / next from the selected event `sel` with the playhead at `t`: while the playhead is on it (from where a jump
 * to it lands to EVENT_MARGIN_S past its end), the event before or after it in time order, so stepping through the
 * camera's events goes one by one, overlapping ones too; once playback (or a seek) has left it, the event before or
 * after the playhead (neighbour), so Next never goes back to footage already played through.
 */
export function stepEvent<E extends Pick<TimelineEvent, "id" | "start_ts" | "end_ts">>(events: E[], sel: E, t: number, dir: -1 | 1, now: number): E | null {
  if (!onEvent(sel, t, now)) return neighbour(events, t, dir);
  const sorted = [...events].sort(byStart);
  const i = sorted.findIndex((x) => x.id === sel.id);
  if (i < 0) return neighbour(events, t, dir);
  return sorted[i + dir] ?? null;
}
/** Is the playhead `t` on event `sel` (from where a jump to it lands to EVENT_MARGIN_S past its end)? */
const onEvent = (sel: Pick<TimelineEvent, "start_ts" | "end_ts">, t: number, now: number): boolean =>
  t >= jumpTarget(sel) - 0.5 && t <= eventEnd(sel, now) + EVENT_MARGIN_S;
type Ordered = Pick<TimelineEvent, "id" | "start_ts">;
/** Time order: by start, then id (stepEvent's order, and the server's sort=oldest). */
const byStart = (a: Ordered, b: Ordered): number => a.start_ts - b.start_ts || a.id - b.id;

/*
 * Previous / next beyond the listing. The listing (listingRange) covers the strip and NEIGHBOUR_RANGE_S either side, so
 * a camera whose previous event is longer ago than that has none in it, and stepping back stopped there (Shawn,
 * 2026-10-23: Prev greyed out after ~8 events). Then the server is asked for the nearest one beyond it (farNeighbour),
 * once per (server, camera, anchor event, direction) (FarCache); the button stays usable while that answer is coming
 * and is greyed out only when the server says there is none.
 */

/** The statuses previous / next can land on: listedEvents' (everything but masked), for asking the server. */
export const STEP_STATUSES = "open,pending,verified,rejected,error";
/** What previous / next goes to: an event in the listing, or (`far`) the server's nearest one before / after `anchor`.
 *  `key` caches that answer: camera, anchor event and direction. */
export type StepPlan<E> = { kind: "listed"; event: E } | { kind: "far"; anchor: E; dir: -1 | 1; key: string };
export const farKey = (camera: string, anchorId: number, dir: -1 | 1): string => `${camera}|${anchorId}|${dir}`;

/**
 * stepEvent, and when the listing has nothing that way, what to ask the server: the nearest event before / after the
 * anchor. With the playhead on `sel` that is `sel` itself; once it has left it (neighbour: by the playhead) the listed
 * event furthest that way (`sel` among them), since the listing has nothing between the playhead and it.
 */
export function stepPlan<E extends Pick<TimelineEvent, "id" | "start_ts" | "end_ts">>(
  events: E[], sel: E, t: number, dir: -1 | 1, now: number, camera: string,
): StepPlan<E> {
  const x = stepEvent(events, sel, t, dir, now);
  if (x) return { kind: "listed", event: x };
  let anchor = sel;
  if (!onEvent(sel, t, now)) {
    const sorted = [...events, sel].sort(byStart);
    anchor = dir < 0 ? sorted[0] : sorted[sorted.length - 1];
  }
  return { kind: "far", anchor, dir, key: farKey(camera, anchor.id, dir) };
}

/** One of the server's answers in the cache: being asked (`prev` = the answer it replaces, if any), the event (null:
 *  none) and when, or the request failed (when). */
export type FarEntry<E> =
  | { state: "pending"; p: Promise<E | null>; prev?: E | null }
  | { state: "done"; event: E | null; at: number }
  | { state: "error"; at: number };
/** "None later" can stop being true (a new event), and "none earlier" rarely (footage put back from an SD card): asked
 *  again after this long (ms). A failed request is asked again after FAR_ERROR_TTL_MS, or at once on a press. */
export const FAR_NONE_TTL_MS = 60_000;
export const FAR_ERROR_TTL_MS = 10_000;
/** Answers kept (the oldest asked go first). */
const FAR_CACHE_MAX = 500;

/** The server's answers for previous / next beyond the listing, by farKey. One per server client (EventPlayer). */
export class FarCache<E> {
  private m = new Map<string, FarEntry<E>>();
  get(key: string): FarEntry<E> | undefined { return this.m.get(key); }
  /** Is `entry` an answer to go by at `nowMs` (else ask again)? */
  static fresh<E>(entry: FarEntry<E> | undefined, nowMs: number): boolean {
    if (!entry) return false;
    if (entry.state === "pending") return true;
    if (entry.state === "error") return nowMs - entry.at < FAR_ERROR_TTL_MS;
    return entry.event != null || nowMs - entry.at < FAR_NONE_TTL_MS;
  }
  /** The answer for `key`: the cached one (`force`: not a failed one), the request under way, or `run` asked now. */
  ask(key: string, run: () => Promise<E | null>, nowMs = Date.now(), force = false): Promise<E | null> {
    const x = this.m.get(key);
    if (x?.state === "pending") return x.p;
    if (x?.state === "done" && FarCache.fresh(x, nowMs)) return Promise.resolve(x.event);
    if (x?.state === "error" && !force && FarCache.fresh(x, nowMs)) return Promise.reject(new Error("asked a moment ago"));
    const p = run().then(
      (event) => { this.set(key, { state: "done", event, at: Date.now() }); return event; },
      (err: unknown) => { this.set(key, { state: "error", at: Date.now() }); throw err; },
    );
    this.set(key, { state: "pending", p, prev: x?.state === "done" ? x.event : undefined });
    return p;
  }
  private set(key: string, v: FarEntry<E>) {
    this.m.delete(key);
    this.m.set(key, v);
    while (this.m.size > FAR_CACHE_MAX) this.m.delete(this.m.keys().next().value as string);
  }
}

/** A previous / next button: the event it goes to (null: not known yet, or none), greyed out (`disabled`: only when
 *  the server said there is none), and whether to ask the server now (`ask`: not asked yet, or the answer is stale).
 *  `listed`: the listing is there (before it is, nothing is asked: it usually has the neighbour). */
export type StepButton<E> = { event: E | null; disabled: boolean; ask: boolean };
export function stepButton<E>(plan: StepPlan<E>, entry: FarEntry<E> | undefined, listed: boolean, nowMs: number): StepButton<E> {
  if (plan.kind === "listed") return { event: plan.event, disabled: false, ask: false };
  const ask = listed && !FarCache.fresh(entry, nowMs);
  if (entry?.state === "done") return { event: entry.event, disabled: entry.event == null, ask };
  if (entry?.state === "pending") return { event: entry.prev ?? null, disabled: entry.prev === null, ask: false };   // refreshing "none": stays greyed
  return { event: null, disabled: false, ask };
}

/** An event from the events API (farNeighbour) as the listing's events are (a TimelineEvent: ticks, previous / next). */
export const asTimelineEvent = (e: NvrEvent): TimelineEvent => ({
  id: e.id, camera_class: e.camera_class, yolo_class: e.yolo_class, yolo_conf: e.yolo_conf, start_ts: e.start_ts, end_ts: e.end_ts,
  status: e.status, threat: e.threat, priority: e.priority, anomaly: e.anomaly, journey_id: e.journey_id, cells: e.cells,
  verdict: e.feedback?.verdict ?? null, has_synopsis: e.synopsis ? 1 : 0, ppe: e.policy?.kind === "ppe" ? 1 : 0,
});

/** The events API's parameters farNeighbour uses (api.ts SiteApi.events, with the camera and STEP_STATUSES added). */
export type FarQuery = { since?: number; until?: number; limit: number; sort?: "oldest"; before_id?: number };
/** Start times are floats: "before / after the anchor" is this much past its start. */
const FAR_EPS_S = 0.001;
/** Earlier events asked for at once (newest first is by id; the nearest by start time is taken among them). */
const FAR_BACK_N = 5;
/** An older server (no sort=oldest): later events are looked for in these windows after the anchor, nearest first,
 *  each paged newest first FAR_PAGE at a time (at most FAR_PAGES pages), the earliest of them taken. */
const FAR_WINDOWS_S = [3600, 86400, 7 * 86400];
export const FAR_PAGE = 500;
const FAR_PAGES = 5;
/** The request was refused as unknown (422: a server before sort=oldest checks the value and refuses it). */
const refused = (err: unknown): boolean => /^422\b/.test(err instanceof Error ? err.message : String(err));

/**
 * The camera's nearest event before (dir -1) or after (dir 1) `anchor` by start time, on the server (`query`: its
 * events API for this camera, masked ones left out); null when there is none.
 * Before: until= just before it, newest first. After: since= just after it with sort=oldest, two of them: one, or two
 * in time order, is the answer. A server without sort=oldest refuses it (422) or, ignoring it, answers newest first
 * (the two out of order): then its events after the anchor are paged in windows, nearest window first (oldServerNext).
 */
export async function farNeighbour<E extends Pick<TimelineEvent, "id" | "start_ts" | "status">>(
  query: (q: FarQuery) => Promise<E[]>, anchor: Ordered, dir: -1 | 1, now: number,
): Promise<E | null> {
  const keep = (rows: E[]) => listedEvents(rows).filter((x) => x.id !== anchor.id && (dir < 0 ? x.start_ts < anchor.start_ts : x.start_ts > anchor.start_ts));
  if (dir < 0) {
    const rows = keep(await query({ until: anchor.start_ts - FAR_EPS_S, limit: FAR_BACK_N }));
    return rows.sort(byStart).pop() ?? null;
  }
  let rows: E[];
  try {
    rows = await query({ since: anchor.start_ts + FAR_EPS_S, limit: 2, sort: "oldest" });
  } catch (err) {
    if (!refused(err)) throw err;
    return oldServerNext(query, anchor, now, keep);
  }
  const got = keep(rows);
  if (got.length <= 1) return got[0] ?? null;
  return byStart(got[0], got[1]) <= 0 ? got[0] : oldServerNext(query, anchor, now, keep);
}

/** The earliest event after `anchor` from a server that lists newest first only: window by window (FAR_WINDOWS_S, then
 *  up to now), each paged down by id. */
async function oldServerNext<E extends Pick<TimelineEvent, "id" | "start_ts" | "status">>(
  query: (q: FarQuery) => Promise<E[]>, anchor: Ordered, now: number, keep: (rows: E[]) => E[],
): Promise<E | null> {
  const edges = [anchor.start_ts + FAR_EPS_S, ...FAR_WINDOWS_S.map((s) => anchor.start_ts + s)].filter((x, i) => i === 0 || x < now);
  for (let w = 0; w < edges.length; w++) {
    const since = edges[w], until = edges[w + 1];   // the last window: up to now
    let best: E | null = null;
    let before: number | undefined;
    for (let page = 0; page < FAR_PAGES; page++) {
      const rows = await query({ since, until, limit: FAR_PAGE, before_id: before });
      for (const x of keep(rows)) if (!best || byStart(x, best) < 0) best = x;
      if (rows.length < FAR_PAGE) break;
      before = Math.min(...rows.map((x) => x.id));
    }
    if (best) return best;
  }
  return null;
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
