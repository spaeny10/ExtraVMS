import { describe, expect, it } from "vitest";
import { NBYTES, cellIndex, encodeCells, regionFetchKey, regionFetchPlan, setCell, type RegionResults } from "./region";

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

  it("hub keys (server/camera) survive the key round trip", () => {
    expect(regionFetchPlan(new Map(), regionFetchKey(["srv1/cam 2"], { "srv1/cam 2": b1 })).stale).toEqual([["srv1/cam 2", encodeCells(b1)]]);
  });
});
