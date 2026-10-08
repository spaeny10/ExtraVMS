import { describe, expect, it } from "vitest";
import { findGaps, fmtLength, onCard, recoverableGaps, restoredLabel, sdStatusText, type RestoredSpan } from "./sdcard";

const T = (h: number, m = 0, s = 0) => new Date(2026, 9, 8, h, m, s).getTime() / 1000;

describe("SD status text", () => {
  it("says what the card holds", () => {
    expect(sdStatusText({ supported: true, has_recording: true, earliest: T(0) - 24 * 86400, latest: T(11), recording_now: true }))
      .toBe("SD card: recording, holds Sep 14 → now");
    expect(sdStatusText({ supported: true, has_recording: true, earliest: T(0) - 24 * 86400, latest: T(11), recording_now: false }))
      .toBe("SD card: not recording, holds Sep 14 → Oct 8");
  });
  it("no recording, no Profile G, not checked", () => {
    expect(sdStatusText({ supported: true, has_recording: false })).toBe("SD card: no recording on the camera");
    expect(sdStatusText({ supported: false, error: "connection failed: timed out" })).toBe("SD card: can't tell (connection failed: timed out)");
    expect(sdStatusText({ supported: false })).toBe("SD card: the camera can't replay its recording");
    expect(sdStatusText(null)).toBe("SD card: not checked yet");
  });
});

describe("gaps", () => {
  const spans = [{ start: T(10), end: T(10, 59, 3) }, { start: T(10, 59, 13), end: T(10, 59, 50) }, { start: T(11, 3, 4), end: T(12) }];
  it("holes of 20 s or more between spans; never before the first", () => {
    expect(findGaps(spans, T(12, 0, 30))).toEqual([{ start: T(10, 59, 50), end: T(11, 3, 4) }]);   // the 10 s hole is ignored
  });
  it("a hole at the live edge counts once it is a minute old", () => {
    expect(findGaps(spans, T(12, 1))).toEqual([{ start: T(10, 59, 50), end: T(11, 3, 4) }]);
    expect(findGaps(spans, T(12, 5))).toEqual([{ start: T(10, 59, 50), end: T(11, 3, 4) }, { start: T(12), end: T(12, 4) }]);
    expect(findGaps([], T(12))).toEqual([]);
  });
  it("clipped to the card", () => {
    const g = { start: T(10), end: T(11) };
    expect(onCard(g, { from: T(10, 30), to: T(12) })).toEqual({ start: T(10, 30), end: T(11) });
    expect(onCard(g, { from: T(11, 30), to: T(12) })).toBeNull();
    expect(onCard(g, null)).toBeNull();
  });
  it("recoverable: on the card and not already taken by a job (a failed one may be retried)", () => {
    const card = { from: T(0), to: T(12, 30) };
    const job = (from_ts: number, to_ts: number, state: RestoredSpan["state"]): RestoredSpan => ({ id: 1, from_ts, to_ts, state, source: "sd" });
    expect(recoverableGaps(spans, card, [], T(12, 0, 30))).toEqual([{ start: T(10, 59, 50), end: T(11, 3, 4) }]);
    expect(recoverableGaps(spans, card, [job(T(10, 59, 50), T(11, 3, 4), "waiting")], T(12, 0, 30))).toEqual([]);
    expect(recoverableGaps(spans, card, [job(T(10, 59, 50), T(11, 3, 4), "failed")], T(12, 0, 30))).toHaveLength(1);
    // a job covering the first half leaves the rest
    expect(recoverableGaps(spans, card, [job(T(10, 59, 50), T(11, 1), "recovered")], T(12, 0, 30))).toEqual([{ start: T(11, 1), end: T(11, 3, 4) }]);
    expect(recoverableGaps(spans, null, [], T(12, 0, 30))).toEqual([]);
  });
});

describe("labels", () => {
  it("restored span text and shade", () => {
    const r = (state: RestoredSpan["state"]): RestoredSpan => ({ id: 1, from_ts: 0, to_ts: 1, state, source: "sd" });
    expect(restoredLabel(r("recovered"))).toEqual({ text: "Recovered from the camera's SD card", cls: "done" });
    expect(restoredLabel(r("recovering")).cls).toBe("busy");
    expect(restoredLabel(r("waiting")).cls).toBe("busy");
    expect(restoredLabel(r("not on the card")).text).toBe("Not on the camera's SD card");
    expect(restoredLabel(r("failed")).cls).toBe("none");
  });
  it("lengths", () => {
    expect(fmtLength(42)).toBe("42 s");
    expect(fmtLength(240)).toBe("4 min");
    expect(fmtLength(250)).toBe("4 min 10 s");
    expect(fmtLength(5400)).toBe("1 h 30 min");
  });
});
