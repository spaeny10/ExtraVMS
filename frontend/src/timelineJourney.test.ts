import { describe, expect, it } from "vitest";
import { gapSkip, journeyCamAt, journeyLead } from "./Timeline";

const PRE = 5;   // FOCUS_PREROLL_S
const lane = (...spans: [number, number][]) => ({ spans: spans.map(([start, end]) => ({ start, end })) });

describe("gapSkip (the clock's gap skip)", () => {
  // A (shown) has footage 0-100 and 1000-1100; the journey goes A (50-60) -> B (300-320) -> A (1050)
  const lanes = { A: lane([0, 100], [1000, 1100]), B: lane([310, 330]) };
  const journey = [{ cam: "A", start: 50, end: 60 }, { cam: "B", start: 300, end: 320 }, { cam: "A", start: 1050, end: 1060 }];

  it("without a journey, jumps to the shown camera's next recording", () => {
    expect(gapSkip(lanes, ["A"], 50, null)).toEqual({ gap: false, next: null });
    expect(gapSkip(lanes, ["A"], 150, null)).toEqual({ gap: true, next: 1000 });   // straight past B's sighting
  });
  it("following a journey, stops at the next sighting's pre-roll", () => {
    expect(gapSkip(lanes, ["A"], 150, journey)).toEqual({ gap: true, next: 300 - PRE });
  });
  it("a sighting under way counts its camera as shown, and the skip moves on from inside it", () => {
    expect(gapSkip(lanes, ["A"], 312, journey)).toEqual({ gap: false, next: null });   // B has footage: play on
    expect(gapSkip(lanes, ["A"], 300 - PRE, journey)).toEqual({ gap: true, next: 310 });   // B's footage starts at 310
    expect(gapSkip(lanes, ["A"], 331, journey)).toEqual({ gap: true, next: 1000 });
  });
  it("an offline camera (no lane) never strands the follower: the next sighting is still a target", () => {
    expect(gapSkip({ A: lane([0, 100]) }, ["A"], 150, journey)).toEqual({ gap: true, next: 300 - PRE });
    expect(gapSkip({ A: lane([0, 100]) }, ["A"], 2000, journey)).toEqual({ gap: true, next: null });
  });
});

describe("journeyLead / journeyCamAt", () => {
  const members = [{ cam: "gone", start: 10, end: 20 }, { cam: "A", start: 50, end: 60 }, { cam: "B", start: 300, end: 320 }];
  it("leads with the first sighting on a camera this Timeline has", () => {
    expect(journeyLead(members, ["A", "B"]).cam).toBe("A");
    expect(journeyLead(members, ["gone", "A"]).cam).toBe("gone");
    expect(journeyLead(members, []).cam).toBe("gone");   // none here: the first, as before
  });
  it("mid-journey: the sighting under way, else the next one, else the lead", () => {
    expect(journeyCamAt(members, 310, ["A", "B"])).toBe("B");
    expect(journeyCamAt(members, 100, ["A", "B"])).toBe("B");   // between sightings: the next
    expect(journeyCamAt(members, 15, ["A", "B"])).toBe("A");    // "gone" isn't here
    expect(journeyCamAt(members, 5000, ["A", "B"])).toBe("A");  // after the last: the lead
    expect(journeyCamAt(members, null, ["A", "B"])).toBe("A");
  });
});

describe("gapSkip with nothing shown", () => {
  it("is a gap with nowhere to go (seekTo says so), as before", () => {
    expect(gapSkip({}, [], 10, null)).toEqual({ gap: true, next: null });
  });
});
