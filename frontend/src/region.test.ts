import { describe, expect, it } from "vitest";
import { NBYTES, cellIndex, encodeCells, regionFetchKey, regionFetchPlan, regionScope, setCell, type RegionResults } from "./region";

const painted = (...cells: number[]) => { const b = new Uint8Array(NBYTES); cells.forEach((c) => setCell(b, c, true)); return b; };

describe("regionFetchPlan (per-camera region fetches)", () => {
  const a1 = painted(cellIndex(1, 1)), a2 = painted(cellIndex(1, 1), cellIndex(2, 1)), b1 = painted(cellIndex(5, 5));
  const have: RegionResults<number> = new Map([["a", { region: encodeCells(a1), items: [1] }], ["b", { region: encodeCells(b1), items: [2] }]]);

  it("repainting one camera refetches only that camera and keeps the other's results", () => {
    const { keep, stale } = regionFetchPlan(have, regionFetchKey(["a", "b"], { a: a2, b: b1 }));
    expect(stale).toEqual([["a", encodeCells(a2)]]);
    expect(keep).toBe(have);   // nothing dropped: the old events of "a" stay until its new ones arrive
  });

  it("an unchanged set fetches nothing; a cleared camera's results are dropped", () => {
    expect(regionFetchPlan(have, regionFetchKey(["a", "b"], { a: a1, b: b1 })).stale).toEqual([]);
    const { keep, stale } = regionFetchPlan(have, regionFetchKey(["b"], { b: b1 }));
    expect(stale).toEqual([]);
    expect([...keep.keys()]).toEqual(["b"]);
  });

  it("a newly painted camera is fetched; nothing painted keeps nothing", () => {
    expect(regionFetchPlan(new Map(), regionFetchKey(["a"], { a: a1 })).stale).toEqual([["a", encodeCells(a1)]]);
    const none = regionFetchPlan(have, regionFetchKey([""], {}));
    expect(none.stale).toEqual([]);
    expect(none.keep.size).toBe(0);
  });

  it("a new variant (the feed's pills) refetches every camera, keeping their events meanwhile", () => {
    const key = regionFetchKey(["a", "b"], { a: a1, b: b1 });
    expect(regionFetchPlan(have, key, "vehicle|").stale).toEqual([["a", encodeCells(a1)], ["b", encodeCells(b1)]]);
    expect(regionFetchPlan(have, key, "vehicle|").keep).toBe(have);
    const fetched: RegionResults<number> = new Map([["a", { region: encodeCells(a1), items: [1], variant: "vehicle|" }]]);
    expect(regionFetchPlan(fetched, regionFetchKey(["a"], { a: a1 }), "vehicle|").stale).toEqual([]);
    expect(regionFetchPlan(fetched, regionFetchKey(["a"], { a: a1 })).stale).toEqual([["a", encodeCells(a1)]]);   // back to no pills
  });

  it("hub keys (server/camera) survive the key round trip", () => {
    expect(regionFetchPlan(new Map(), regionFetchKey(["srv1/cam 2"], { "srv1/cam 2": b1 })).stale).toEqual([["srv1/cam 2", encodeCells(b1)]]);
  });
});

describe("regionScope (the event viewer's previous / next kept to a painted region)", () => {
  const region = painted(cellIndex(3, 3));
  const through = encodeCells(painted(cellIndex(3, 3), cellIndex(4, 3)));
  const elsewhere = encodeCells(painted(cellIndex(20, 10)));
  const evs = [
    { id: 1, status: "verified", cells: through },
    { id: 2, status: "verified", cells: elsewhere },
    { id: 3, status: "open", cells: null },                                 // still open: its path isn't complete yet
    { id: 4, status: "verified", cells: through, ptz_preset: "away" },      // camera turned away: can't match
    { id: 5, status: "pending", cells: null },
  ];

  it("keeps the events through the region, asks the server for those only, and tags its cached answers", () => {
    const s = regionScope(region);
    expect(s.keep(evs).map((e) => e.id)).toEqual([1, 3]);
    expect(s.query).toEqual({ region: encodeCells(region) });
    expect(s.tag).not.toBe("");
    expect(regionScope(painted(cellIndex(9, 9))).tag).not.toBe(s.tag);   // another region: other answers
  });

  it("no region (or an empty one): everything, nothing added to the query", () => {
    for (const s of [regionScope(undefined), regionScope(new Uint8Array(NBYTES))]) {
      expect(s.keep(evs)).toBe(evs);
      expect(s.query).toEqual({});
      expect(s.tag).toBe("");
    }
  });
});
