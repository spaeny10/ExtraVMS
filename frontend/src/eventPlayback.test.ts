import { describe, expect, it } from "vitest";
import type { TimelineEvent } from "./api";
import {
  CLIP_PRE_ROLL_S, EDGE_S, EVENT_MARGIN_S, JUMP_LEAD_S, MID_STALL_S, NEAR_END_S, NEIGHBOUR_RANGE_S, PRE_ROLL_S, START_TIMEOUT_S,
  STRIP_FILL, STRIP_HALF_S, STUCK_S, ZOOM_HALF_S, afterPlaybackError, atFrac, chunkAt, chunkStep, clipOrigin, coverage,
  defaultQuality, eventEnd, firstRecorded, followWindow, footageAvailable, initialSource, jumpTarget, listingRange, nearChunkEnd,
  needsListing, neighbour, nextChunkStart, pauseAtEnd, playStart, prefetchLead, PREV_SLACK_S, relLabel, scaleMarks,
  snapToRecorded, stripWindow, ticks, toFrac, viaHub, zoomLabel, zoomWindow, type ChunkLook,
} from "./eventPlayback";
import {
  CHUNK, FAR_CHUNK, FIRST_FAR_CHUNK, PREFETCH_LEAD_S, REMOTE_CHUNK, REMOTE_FIRST_CHUNK, mergeSpans, nowS,
} from "./playback";

const NOW = Math.floor(nowS());   // chunk lengths (playback.ts isFar) read the real clock
const OLD = NOW - 3 * 86400;   // far back: short first chunks

const ev = (id: number, start_ts: number, extra: Partial<TimelineEvent> = {}): TimelineEvent => ({
  id, start_ts, end_ts: start_ts + 10, camera_class: "person", yolo_class: "person", yolo_conf: 0.9, status: "verified", threat: null,
  priority: null, anomaly: null, journey_id: null, cells: null, verdict: null, has_synopsis: 1, ...extra,
} as TimelineEvent);

describe("where playback starts", () => {
  it("10 s before the event, or at the clip's start when that is earlier", () => {
    expect(playStart({ start_ts: 1000, clip_start: 995 })).toBe(1000 - PRE_ROLL_S);
    expect(playStart({ start_ts: 1000, clip_start: 980 })).toBe(980);
  });
  it("clip time 0 is clip_start, else the server's pre-roll before the event (an older server)", () => {
    expect(clipOrigin({ start_ts: 1000, clip_start: 993 })).toBe(993);
    expect(clipOrigin({ start_ts: 1000, clip_start: null })).toBe(1000 - CLIP_PRE_ROLL_S);
    expect(clipOrigin({ start_ts: 1000 })).toBe(1000 - CLIP_PRE_ROLL_S);
  });
  it("an event still being tracked ends now", () => {
    expect(eventEnd({ start_ts: 1000, end_ts: 1030 }, NOW)).toBe(1030);
    expect(eventEnd({ start_ts: 1000, end_ts: null }, 1200)).toBe(1200);
  });
});

describe("the strip's window", () => {
  it("±1 min around a short event by default, so a 7 s event is clearly there", () => {
    const w = stripWindow(OLD, OLD + 7, NOW);
    expect(STRIP_HALF_S).toBe(60);
    expect(w).toEqual({ start: OLD - 60, end: OLD + 7 + 60 });
    expect(toFrac(OLD + 7, w) - toFrac(OLD, w)).toBeGreaterThan(0.05);   // was under 1.2% of a 10-minute strip
  });
  it("zoomed out: ±5 min, ±15 min", () => {
    expect(stripWindow(OLD, OLD + 20, NOW, 1)).toEqual({ start: OLD - 300, end: OLD + 20 + 300 });
    expect(stripWindow(OLD, OLD + 20, NOW, 2)).toEqual({ start: OLD - 900, end: OLD + 20 + 900 });
    expect(stripWindow(OLD, OLD + 20, NOW, 9)).toEqual(stripWindow(OLD, OLD + 20, NOW, ZOOM_HALF_S.length - 1));   // clamped
    expect([0, 1, 2].map(zoomLabel)).toEqual(["±1 min", "±5 min", "±15 min"]);
  });
  it("a long event plus its margins fills 60% of it", () => {
    const w = stripWindow(OLD, OLD + 1200, NOW);
    const core = 1200 + 2 * EVENT_MARGIN_S;
    expect(w.end - w.start).toBeCloseTo(core / STRIP_FILL);
    expect((toFrac(OLD + 1200 + EVENT_MARGIN_S, w) - toFrac(OLD - EVENT_MARGIN_S, w))).toBeCloseTo(STRIP_FILL);
  });
  it("a long event keeps the same scale when zoomed in (it is wider than ±1 min anyway)", () => {
    expect(stripWindow(OLD, OLD + 1200, NOW, 0)).toEqual(stripWindow(OLD, OLD + 1200, NOW, 1));
  });
  it("a recent event shows more of what led up to it, not empty future, and keeps its end in view", () => {
    const w = stripWindow(NOW - 60, NOW - 40, NOW);
    expect(w.end).toBeLessThanOrEqual(NOW + 30);
    expect(w.end - w.start).toBe(20 + 2 * STRIP_HALF_S);
    const live = stripWindow(NOW - 20, NOW, NOW);
    expect(live.end).toBeGreaterThanOrEqual(NOW + EVENT_MARGIN_S);
  });
  it("follows the playhead out of either edge, same width", () => {
    const w = { start: 0, end: 600 };
    expect(followWindow(w, 300)).toBe(w);
    expect(followWindow(w, 700)).toEqual({ start: 580, end: 1180 });   // ahead: 20% in from the left
    expect(followWindow(w, -100)).toEqual({ start: -580, end: 20 });   // behind: 80% across
  });
  it("zooming keeps the playhead in view: around the event when it is there, else centred on the playhead", () => {
    const ev0 = { start: OLD, end: OLD + 7 };
    expect(zoomWindow(ev0, 1, OLD + 30, NOW)).toEqual(stripWindow(OLD, OLD + 7, NOW, 1));
    // played on 4 minutes past the event, then zoomed back in to ±1 min: the window follows the playhead
    const z = zoomWindow(ev0, 0, OLD + 240, NOW);
    expect(z.end - z.start).toBeCloseTo(127);
    expect((z.start + z.end) / 2).toBeCloseTo(OLD + 240);
    // ...but not far past now
    const r = zoomWindow({ start: NOW - 2000, end: NOW - 1990 }, 0, NOW - 5, NOW);
    expect(r.end).toBeLessThanOrEqual(NOW + 30);
    expect(r.start).toBeLessThan(NOW - 5);
  });
  it("lists at least ±30 min beyond the window, more when zoomed out, and again when the window leaves that", () => {
    const w0 = stripWindow(OLD, OLD + 7, NOW, 0);
    const l0 = listingRange(w0, NOW);
    expect(l0).toEqual({ from: w0.start - NEIGHBOUR_RANGE_S, to: w0.end + NEIGHBOUR_RANGE_S });
    expect(needsListing(undefined, w0, NOW)).toBe(true);
    expect(needsListing(l0, w0, NOW)).toBe(false);
    expect(needsListing(l0, followWindow(w0, w0.end + 600), NOW)).toBe(false);   // played on 10 minutes: still enough
    expect(needsListing(l0, followWindow(w0, w0.end + 1500), NOW)).toBe(true);
    // zoomed out to ±15 min: list wider, so previous / next still reach beyond the strip
    const w2 = zoomWindow({ start: OLD, end: OLD + 7 }, 2, OLD, NOW);
    expect(needsListing(l0, w2, NOW)).toBe(true);
    const l2 = listingRange(w2, NOW);
    expect(w2.start - l2.from).toBeGreaterThanOrEqual(w2.end - w2.start);
    expect(needsListing(l2, w2, NOW)).toBe(false);
    // a listing up to now covers what comes after it
    const wr = stripWindow(NOW - 100, NOW - 90, NOW);
    expect(needsListing(listingRange(wr, NOW), wr, NOW)).toBe(false);
    expect(listingRange(wr, NOW).to).toBe(NOW);
  });
  it("labels round times between the ends when there is room", () => {
    const base = Math.floor(OLD / 3600) * 3600;
    const w = { start: base - 60, end: base + 67 };   // ±1 min around a 7 s event
    expect(scaleMarks(w, 0)).toEqual([]);
    const two = scaleMarks(w, 2);
    expect(two.length).toBeGreaterThan(0);
    expect(two.length).toBeLessThanOrEqual(2);
    for (const m of two) {
      expect(m.t % 30).toBe(0);
      expect(m.frac).toBeGreaterThanOrEqual(0.12);
      expect(m.frac).toBeLessThanOrEqual(0.88);
    }
    const many = scaleMarks(w, 8);
    expect(many.length).toBeGreaterThan(two.length);
    expect(many[0].label).toMatch(/^\d\d:\d\d:\d\d$/);   // under a minute apart: with seconds
    expect(scaleMarks(w, 8, 0.3).every((m) => m.frac >= 0.3 && m.frac <= 0.7)).toBe(true);   // clear of wide end labels
    const wide = scaleMarks({ start: base - 900, end: base + 907 }, 3);
    expect(wide.length).toBeGreaterThan(0);
    expect(wide.every((m) => /^\d\d:\d\d$/.test(m.label) && m.t % 300 === 0)).toBe(true);
  });
  it("positions and times across it, clamped", () => {
    const w = { start: 100, end: 200 };
    expect(toFrac(150, w)).toBe(0.5);
    expect(toFrac(50, w)).toBe(0);
    expect(toFrac(250, w)).toBe(1);
    expect(atFrac(0.25, w)).toBe(125);
    expect(atFrac(-1, w)).toBe(100);
    expect(atFrac(2, w)).toBe(200);
  });
});

describe("recorded and not recorded", () => {
  const w = { start: 0, end: 100 };
  it("cuts the window into pieces that cover it in order", () => {
    const c = coverage([{ start: 20, end: 40 }, { start: 60, end: 130 }], w);
    expect(c).toEqual([
      { start: 0, end: 20, recorded: false }, { start: 20, end: 40, recorded: true },
      { start: 40, end: 60, recorded: false }, { start: 60, end: 100, recorded: true },
    ]);
  });
  it("nothing recorded is one gap; all recorded is one piece", () => {
    expect(coverage([], w)).toEqual([{ start: 0, end: 100, recorded: false }]);
    expect(coverage([{ start: -50, end: 500 }], w)).toEqual([{ start: 0, end: 100, recorded: true }]);
  });
  it("only the keep-window around an old event: gaps either side", () => {
    const c = coverage([{ start: 45, end: 70 }], w);
    expect(c.map((p) => p.recorded)).toEqual([false, true, false]);
  });
  it("unsorted and overlapping spans don't double up", () => {
    const c = coverage([{ start: 50, end: 80 }, { start: 10, end: 60 }], w);
    expect(c).toEqual([{ start: 0, end: 10, recorded: false }, { start: 10, end: 60, recorded: true }, { start: 60, end: 80, recorded: true }, { start: 80, end: 100, recorded: false }]);
  });
  it("the listing's segments become spans with sub-2 s holes joined (shared with the Timeline)", () => {
    const iso = (t: number) => new Date(t * 1000).toISOString();
    expect(mergeSpans([{ start: iso(OLD + 60), duration: 60 }, { start: iso(OLD), duration: 59 }, { start: iso(OLD + 200), duration: 10 }]))
      .toEqual([{ start: OLD, end: OLD + 120 }, { start: OLD + 200, end: OLD + 210 }]);
  });
  it("the first recorded moment from playback's start up to the event's end", () => {
    const spans = [{ start: 100, end: 200 }];
    expect(firstRecorded(spans, 150, 300)).toBe(150);
    expect(firstRecorded(spans, 90, 300)).toBe(100 + EDGE_S);   // the keep-window starts after the pre-roll; a little into it
    expect(firstRecorded(spans, 210, 300)).toBeNull();
    expect(firstRecorded(spans, 50, 90)).toBeNull();
  });
  it("a tap in a gap goes to the next recording, else the end of the last one", () => {
    const spans = [{ start: 100, end: 200 }, { start: 300, end: 400 }];
    expect(snapToRecorded(spans, 150)).toBe(150);
    expect(snapToRecorded(spans, 250)).toBe(300 + EDGE_S);
    expect(snapToRecorded(spans, 50)).toBe(100 + EDGE_S);
    expect(snapToRecorded([{ start: 100, end: 100.4 }], 50)).toBe(100.2);   // a sliver: its middle
    expect(snapToRecorded(spans, 450)).toBe(399);
    expect(snapToRecorded([], 450)).toBeNull();
    // stepping back into a gap: the end of the recording before it (else, at the very start, the next one)
    expect(snapToRecorded(spans, 250, -1)).toBe(199);
    expect(snapToRecorded(spans, 50, -1)).toBe(100 + EDGE_S);
  });
});

describe("the camera's other events", () => {
  const w = { start: 1000, end: 1600 };
  const label = (e: TimelineEvent) => `${e.camera_class} ${e.id}`;
  it("ticks inside the window, in order, without the viewer's own event", () => {
    const t = ticks([ev(3, 1450), ev(1, 1300), ev(2, 1100), ev(9, 2000), ev(8, 900)], w, 1, label);
    expect(t.map((x) => x.id)).toEqual([2, 3]);
    expect(t[0].frac).toBeCloseTo(100 / 600);
    expect(t[1]).toMatchObject({ cls: "person verified", title: "person 3" });
  });
  it("a rejected vehicle's tick carries both classes", () => {
    expect(ticks([ev(4, 1200, { camera_class: "vehicle", status: "rejected" })], w, 1, label)[0].cls).toBe("vehicle rejected");
  });
  it("previous / next by where a jump lands, with some slack", () => {
    const all = [ev(1, 1000), ev(2, 1100), ev(3, 1200)];
    expect(neighbour(all, 1150, -1)?.id).toBe(2);
    expect(neighbour(all, 1150, 1)?.id).toBe(3);
    // right after a jump to #2 (its start minus the lead), and a few seconds into it, previous is #1, not #2 again
    expect(neighbour(all, jumpTarget(all[1]), -1)?.id).toBe(1);
    expect(neighbour(all, jumpTarget(all[1]) + PREV_SLACK_S - 0.5, -1)?.id).toBe(1);
    expect(neighbour(all, jumpTarget(all[1]) + PREV_SLACK_S + 0.5, -1)?.id).toBe(2);
    expect(neighbour(all, jumpTarget(all[1]), 1)?.id).toBe(3);
    expect(neighbour(all, 900, -1)).toBeNull();
    expect(neighbour(all, 1300, 1)).toBeNull();
    expect(jumpTarget(all[0])).toBe(1000 - JUMP_LEAD_S);
  });
  it("next finds a later event in the listing beyond the strip, whatever its class or status", () => {
    // the viewer's own 7 s event, playing 4 s past its end; the listing (±30 min) has later events off the strip
    const own = ev(1, OLD, { end_ts: OLD + 7 });
    const later = [ev(2, OLD + 900, { camera_class: "vehicle", status: "rejected" }), ev(3, OLD + 1500)];
    expect(neighbour([own, ...later], OLD + 11, 1)?.id).toBe(2);
    expect(neighbour([own, ...later], OLD + 11, -1)?.id).toBe(1);
    expect(neighbour([own], OLD + 11, 1)).toBeNull();
  });
});

describe("recording chunks", () => {
  it("a short first chunk far back, full ones when continuing (the Timeline's lengths)", () => {
    expect(chunkAt(OLD, null, { remote: false, cont: false, now: NOW })).toEqual({ start: OLD, len: FIRST_FAR_CHUNK });
    expect(chunkAt(OLD, null, { remote: false, cont: true, now: NOW }).len).toBe(FAR_CHUNK);
    expect(chunkAt(NOW - 120, null, { remote: false, cont: false, now: NOW }).len).toBe(CHUNK);
  });
  it("through the hub: the remote lengths", () => {
    expect(chunkAt(OLD, null, { remote: true, cont: false, now: NOW }).len).toBe(REMOTE_FIRST_CHUNK);
    expect(chunkAt(OLD, null, { remote: true, cont: true, now: NOW }).len).toBe(REMOTE_CHUNK);
  });
  it("never runs past the end of the recorded span it starts in", () => {
    const spans = [{ start: OLD, end: OLD + 12 }, { start: OLD + 100, end: OLD + 400 }];
    expect(chunkAt(OLD + 2, spans, { remote: false, cont: true, now: NOW }).len).toBe(10);
    expect(chunkAt(OLD + 11.8, spans, { remote: false, cont: true, now: NOW }).len).toBe(1);   // at least a second
    expect(chunkAt(OLD + 100, spans, { remote: false, cont: true, now: NOW }).len).toBe(FAR_CHUNK);
  });
  it("the span still growing at the live edge isn't cut", () => {
    const spans = [{ start: NOW - 3600, end: NOW - 5 }];
    expect(chunkAt(NOW - 300, spans, { remote: false, cont: false, now: NOW }).len).toBe(CHUNK);
  });
  it("continues right after a chunk, skips a gap to the next recording, stops at the end or the live edge", () => {
    const spans = [{ start: OLD, end: OLD + 100 }, { start: OLD + 300, end: OLD + 400 }];
    expect(nextChunkStart(OLD, 20, spans, NOW)).toBeCloseTo(OLD + 20.1);
    expect(nextChunkStart(OLD + 80, 20, spans, NOW)).toBe(OLD + 300 + EDGE_S);
    expect(nextChunkStart(OLD + 380, 20, spans, NOW)).toBeNull();
    expect(nextChunkStart(OLD, 20, null, NOW)).toBeCloseTo(OLD + 20.1);   // listing unknown: just carry on
    expect(nextChunkStart(NOW - 30, 28, null, NOW)).toBeNull();
  });
  it("hub-proxied media is remote; direct or local isn't", () => {
    expect(viaHub("/s/fred-001/api/playback/cam1?start=1")).toBe(true);
    expect(viaHub("/api/playback/cam1?start=1")).toBe(false);
    expect(viaHub("https://10-0-0-5.fred.lan.axiomvision.ai:8443/api/playback/cam1?direct=x")).toBe(false);
  });
});

describe("SD or HD", () => {
  it("SD on a phone when the server can transcode, else HD", () => {
    expect(defaultQuality(true, true)).toBe("sd");
    expect(defaultQuality(true, false)).toBe("hd");
    expect(defaultQuality(false, true)).toBe("hd");
  });
  it("a phone waits for the answer; a wide screen doesn't", () => {
    expect(defaultQuality(true, undefined)).toBeNull();
    expect(defaultQuality(false, undefined)).toBe("hd");
  });
});

describe("what to play when", () => {
  const spans = [{ start: 100, end: 200 }];
  it("waits for the listing, then the recording from the first recorded moment", () => {
    expect(initialSource({ spans: undefined, from: 90, until: 150, hasClip: true })).toBeNull();
    expect(initialSource({ spans, from: 90, until: 150, hasClip: true })).toEqual({ kind: "recording", at: 100 + EDGE_S });
    expect(initialSource({ spans, from: 120, until: 150, hasClip: false })).toEqual({ kind: "recording", at: 120 });
  });
  it("a failed listing tries the recording anyway", () => {
    expect(initialSource({ spans: null, from: 90, until: 150, hasClip: false })).toEqual({ kind: "recording", at: 90 });
  });
  it("no recording of the event: its own clip, else the snapshot", () => {
    expect(initialSource({ spans: [], from: 90, until: 150, hasClip: true })).toEqual({ kind: "clip" });
    expect(initialSource({ spans: [{ start: 300, end: 400 }], from: 90, until: 150, hasClip: false })).toEqual({ kind: "still" });
  });
  it("a chunk that fails: SD out of slots goes HD; before anything played the clip, then the snapshot; after, once more, then stop", () => {
    expect(afterPlaybackError({ status: 503, quality: "sd", played: true, hasClip: true })).toBe("hd");
    // a continuation refused SD: the chunk before and a dropped prefetch may still hold slots, so ask once more first
    expect(afterPlaybackError({ status: 503, quality: "sd", played: true, hasClip: true, cont: true })).toBe("retry");
    expect(afterPlaybackError({ status: 503, quality: "sd", played: true, hasClip: true, cont: true, retried: true })).toBe("hd");
    expect(afterPlaybackError({ status: 501, quality: "sd", played: true, hasClip: true, cont: true })).toBe("hd");
    expect(afterPlaybackError({ status: null, quality: "hd", played: true, hasClip: true, cont: true, retried: true })).toBe("stop");
    expect(afterPlaybackError({ status: 501, quality: "sd", played: false, hasClip: false })).toBe("hd");
    expect(afterPlaybackError({ status: 503, quality: "hd", played: false, hasClip: true })).toBe("clip");
    expect(afterPlaybackError({ status: 404, quality: "hd", played: false, hasClip: true })).toBe("clip");
    expect(afterPlaybackError({ status: 404, quality: "sd", played: false, hasClip: false })).toBe("still");
    expect(afterPlaybackError({ status: null, quality: "hd", played: false, hasClip: false })).toBe("still");
    expect(afterPlaybackError({ status: 404, quality: "hd", played: true, hasClip: true })).toBe("retry");
    expect(afterPlaybackError({ status: 404, quality: "hd", played: true, hasClip: true, retried: true })).toBe("stop");
  });
  it("the Ask tab: footage is there with the clip or a recording overlapping the event", () => {
    expect(footageAvailable({ spans: undefined, start: 100, end: 150, hasClip: true })).toBe(true);
    expect(footageAvailable({ spans: undefined, start: 100, end: 150, hasClip: false })).toBeUndefined();
    expect(footageAvailable({ spans: null, start: 100, end: 150, hasClip: false })).toBe(true);
    expect(footageAvailable({ spans: [{ start: 140, end: 300 }], start: 100, end: 150, hasClip: false })).toBe(true);
    expect(footageAvailable({ spans: [{ start: 160, end: 300 }], start: 100, end: 150, hasClip: false })).toBe(false);
  });
});

describe("the time label", () => {
  it("before, in and after the event", () => {
    expect(relLabel(92, 100, 130)).toBe("8 s before");
    expect(relLabel(112, 100, 130)).toBe("12 s in");
    expect(relLabel(100, 100, 130)).toBe("0 s in");
    expect(relLabel(195, 100, 130)).toBe("1:05 after");
    expect(relLabel(130 + 59.6, 100, 130)).toBe("1:00 after");
  });
});

describe("handing over from chunk to chunk", () => {
  // a 20 s first chunk, playing, nothing odd: the baseline each case changes
  const look = (o: Partial<ChunkLook> = {}): ChunkLook => ({
    ct: 8, len: 20, duration: 10.4, bufferedEnd: 10.4, idleS: 0, sinceLoadS: 9, started: true,
    wantPlay: true, paused: false, ended: false, loading: true, remote: false, ...o,
  });
  it("plays on while the playhead moves", () => {
    expect(chunkStep(look())).toBe("ok");
    expect(chunkStep(look({ ct: 19.6, idleS: STUCK_S - 0.5 }))).toBe("ok");   // at the end but only just stopped
  });
  it("on to the next chunk when it ended", () => {
    expect(chunkStep(look({ ct: 20.06, duration: 20.06, ended: true, paused: true }))).toBe("advance");
  });
  it("on to the next chunk when it sits at its end without 'ended' (the phone at 0:18 / 0:19)", () => {
    // the SD transcode's duration says 19.06 s, its video ends at 18.95 s: the element waits there for good
    const stuck = look({ ct: 18.95, duration: 19.06, bufferedEnd: 18.95, idleS: STUCK_S, loading: false });
    expect(chunkStep(stuck)).toBe("advance");
    // ...also paused there (Chrome on Android pauses before 'ended'), and while the response is still open
    expect(chunkStep({ ...stuck, paused: true })).toBe("advance");
    expect(chunkStep({ ...stuck, loading: true })).toBe("advance");   // within NEAR_END_S of the length asked for
  });
  it("a transcode much shorter than asked: its end is where the browser's data and duration end, once nothing more comes", () => {
    const short = look({ ct: 12.9, len: 60, duration: 13, bufferedEnd: 12.95, idleS: 2, loading: false });
    expect(nearChunkEnd(short)).toBe(true);
    expect(chunkStep(short)).toBe("advance");
    expect(chunkStep({ ...short, loading: true })).toBe("ok");   // still arriving: wait for it
    expect(nearChunkEnd({ ...short, duration: Infinity })).toBe(false);
    expect(nearChunkEnd({ ...short, bufferedEnd: 12.9 + NEAR_END_S + 1, duration: 12.9 + NEAR_END_S + 1 })).toBe(false);   // more to play
  });
  it("buffering mid-way is waited out, then asked for again from there", () => {
    const waiting = look({ ct: 30, len: 60, duration: 30.4, bufferedEnd: 30.05, loading: true });
    expect(chunkStep({ ...waiting, idleS: 3 })).toBe("ok");
    expect(chunkStep({ ...waiting, idleS: MID_STALL_S })).toBe("reload");
  });
  it("paused but wanted playing (autoplay refused, the browser paused it): play again", () => {
    expect(chunkStep(look({ ct: 0, paused: true, idleS: STUCK_S }))).toBe("play");
    expect(chunkStep(look({ ct: 5, paused: true, idleS: 30, wantPlay: false }))).toBe("ok");   // the viewer paused it
    expect(chunkStep(look({ ct: 18.9, duration: 19.06, paused: true, idleS: 60, wantPlay: false }))).toBe("ok");
  });
  it("a chunk with no answer: asked for again after START_TIMEOUT_S, longer through the hub", () => {
    const fresh = look({ started: false, ct: 0, duration: NaN, bufferedEnd: 0 });
    expect(chunkStep({ ...fresh, sinceLoadS: START_TIMEOUT_S.local - 0.5 })).toBe("ok");
    expect(chunkStep({ ...fresh, sinceLoadS: START_TIMEOUT_S.local })).toBe("retry");
    expect(chunkStep({ ...fresh, sinceLoadS: START_TIMEOUT_S.local, remote: true })).toBe("ok");
    expect(chunkStep({ ...fresh, sinceLoadS: START_TIMEOUT_S.remote, remote: true })).toBe("retry");
  });
  it("the hub's short remote chunks hand over the same way", () => {
    const first = chunkAt(OLD, null, { remote: true, cont: false, now: NOW });
    expect(first.len).toBe(REMOTE_FIRST_CHUNK);
    // stuck a little short of its 20 s: the next starts right where it stopped, at the remote continuation length
    const stuck = look({
      ct: REMOTE_FIRST_CHUNK - 1.1, len: first.len, duration: REMOTE_FIRST_CHUNK - 0.9, bufferedEnd: REMOTE_FIRST_CHUNK - 1.1,
      idleS: 2, loading: false, remote: true,
    });
    expect(chunkStep(stuck)).toBe("advance");
    const at = nextChunkStart(OLD, stuck.ct, null, NOW)!;
    expect(at).toBeCloseTo(OLD + REMOTE_FIRST_CHUNK - 1.0);
    expect(chunkAt(at, null, { remote: true, cont: true, now: NOW }).len).toBe(REMOTE_CHUNK);
    expect(chunkStep(look({ ct: REMOTE_CHUNK - NEAR_END_S, len: REMOTE_CHUNK, duration: REMOTE_CHUNK + 0.2, idleS: 2, remote: true }))).toBe("advance");
  });
  it("a pause at the chunk's end isn't the viewer's: the wish to play carries into the next chunk", () => {
    expect(pauseAtEnd({ ct: 18.95, len: 20, duration: 19.06, ended: false })).toBe(true);   // Android: pause, then ended
    expect(pauseAtEnd({ ct: 13, len: 60, duration: 13.1, ended: false })).toBe(true);
    expect(pauseAtEnd({ ct: 20.06, len: 20, duration: 20.06, ended: true })).toBe(true);
    expect(pauseAtEnd({ ct: 8, len: 20, duration: 10.4, ended: false })).toBe(false);
    expect(pauseAtEnd({ ct: 8, len: 20, duration: Infinity, ended: false })).toBe(false);
  });
  it("prefetches the next chunk early enough for a transcode to arrive", () => {
    expect(prefetchLead(REMOTE_FIRST_CHUNK, PREFETCH_LEAD_S)).toBe(PREFETCH_LEAD_S);
    expect(prefetchLead(REMOTE_CHUNK, PREFETCH_LEAD_S)).toBe(30);
    expect(prefetchLead(FAR_CHUNK, PREFETCH_LEAD_S)).toBe(60);
    expect(prefetchLead(CHUNK, PREFETCH_LEAD_S)).toBe(60);
  });
});
