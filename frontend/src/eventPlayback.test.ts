import { describe, expect, it } from "vitest";
import type { TimelineEvent } from "./api";
import {
  CLIP_PRE_ROLL_S, EDGE_S, EVENT_MARGIN_S, JUMP_LEAD_S, PRE_ROLL_S, STRIP_FILL, STRIP_HALF_S, afterPlaybackError, atFrac, chunkAt,
  clipOrigin, coverage, defaultQuality, eventEnd, firstRecorded, followWindow, footageAvailable, initialSource, jumpTarget,
  neighbour, nextChunkStart, playStart, PREV_SLACK_S, relLabel, snapToRecorded, stripWindow, ticks, toFrac, viaHub,
} from "./eventPlayback";
import { CHUNK, FAR_CHUNK, FIRST_FAR_CHUNK, REMOTE_CHUNK, REMOTE_FIRST_CHUNK, mergeSpans, nowS } from "./playback";

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
  it("about ±5 min around a short event", () => {
    const w = stripWindow(OLD, OLD + 20, NOW);
    expect(w.end - w.start).toBe(2 * STRIP_HALF_S);
    expect((w.start + w.end) / 2).toBe(OLD + 10);   // centred on the event
  });
  it("a long event plus its margins fills 60% of it", () => {
    const w = stripWindow(OLD, OLD + 1200, NOW);
    const core = 1200 + 2 * EVENT_MARGIN_S;
    expect(w.end - w.start).toBeCloseTo(core / STRIP_FILL);
    expect((toFrac(OLD + 1200 + EVENT_MARGIN_S, w) - toFrac(OLD - EVENT_MARGIN_S, w))).toBeCloseTo(STRIP_FILL);
  });
  it("a recent event shows more of what led up to it, not empty future, and keeps its end in view", () => {
    const w = stripWindow(NOW - 60, NOW - 40, NOW);
    expect(w.end).toBeLessThanOrEqual(NOW + 30);
    expect(w.end - w.start).toBe(2 * STRIP_HALF_S);
    const live = stripWindow(NOW - 20, NOW, NOW);
    expect(live.end).toBeGreaterThanOrEqual(NOW + EVENT_MARGIN_S);
  });
  it("follows the playhead out of either edge, same width", () => {
    const w = { start: 0, end: 600 };
    expect(followWindow(w, 300)).toBe(w);
    expect(followWindow(w, 700)).toEqual({ start: 580, end: 1180 });   // ahead: 20% in from the left
    expect(followWindow(w, -100)).toEqual({ start: -580, end: 20 });   // behind: 80% across
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
  it("a chunk that fails: SD out of slots goes HD; before anything played the clip, then the snapshot; after, stop", () => {
    expect(afterPlaybackError({ status: 503, quality: "sd", played: true, hasClip: true })).toBe("hd");
    expect(afterPlaybackError({ status: 501, quality: "sd", played: false, hasClip: false })).toBe("hd");
    expect(afterPlaybackError({ status: 503, quality: "hd", played: false, hasClip: true })).toBe("clip");
    expect(afterPlaybackError({ status: 404, quality: "hd", played: false, hasClip: true })).toBe("clip");
    expect(afterPlaybackError({ status: 404, quality: "sd", played: false, hasClip: false })).toBe("still");
    expect(afterPlaybackError({ status: null, quality: "hd", played: false, hasClip: false })).toBe("still");
    expect(afterPlaybackError({ status: 404, quality: "hd", played: true, hasClip: true })).toBe("stop");
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
