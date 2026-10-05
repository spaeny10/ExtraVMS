import { describe, expect, it } from "vitest";
import type { NvrEvent } from "@site/api";
import type { FleetEvent } from "@site/dashboard/types";
import { NBYTES, cellIndex, encodeCells, setCell } from "@site/region";
import { alertEvent, applyLiveEvent, cameraNameFor, mergePool, poolKey, regionFeed, removeLiveEvent, siteRegionKeys, tagServer,
  timelineTargetHref } from "./eventOpen";

const ev = (server: string, id: number, cam: string, start: number, extra: Partial<FleetEvent> = {}): FleetEvent =>
  ({ id, camera_id: cam, start_ts: start, end_ts: start + 5, status: "verified", camera_class: "person", site_id: server, site_name: server.toUpperCase(), ...extra }) as FleetEvent;

const bits = (...cells: [number, number][]) => { const b = new Uint8Array(NBYTES); for (const [c, r] of cells) setCell(b, cellIndex(c, r), true); return b; };

describe("timelineTargetHref (the viewer's Open in Timeline)", () => {
  it("opens the event on the Site's combined Timeline", () => {
    expect(timelineTargetHref("loc1", "srvA", { id: 42, camera_id: "cam1", start_ts: 100, end_ts: 110 }))
      .toBe("/sites/loc1/timeline?server=srvA&cam=cam1&event=42");
  });
  it("keeps a journey, keyed by its first camera", () => {
    const t = { id: 42, camera_id: "cam2", start_ts: 100, end_ts: 110, members: [{ id: 41, cam: "cam1", start: 90, end: 95 }, { id: 42, cam: "cam2", start: 100, end: 110 }] };
    expect(timelineTargetHref("loc1", "srvA", t)).toBe("/sites/loc1/timeline?server=srvA&cam=cam1&event=42&journey=1");
  });
  it("falls back to the server's console without a Site", () => {
    expect(timelineTargetHref(null, "srvB", { id: 7, camera_id: "cam1", start_ts: 100, end_ts: null })).toBe("/s/srvB/#timeline?cam=cam1&event=7");
    expect(timelineTargetHref(undefined, "srvB", { id: 0, camera_id: "cam1", start_ts: 100.4, end_ts: null })).toBe("/s/srvB/#timeline?cam=cam1&t=100");
  });
});

describe("alertEvent", () => {
  it("reads the event of an event alert", () => {
    expect(alertEvent({ site_id: "srvA", location_id: "loc1", detail: { id: 42, camera_id: "cam1" } })).toEqual({ server: "srvA", id: 42, location: "loc1" });
    expect(alertEvent({ site_id: "srvA", detail: { id: "42", camera_id: "cam1" } })).toEqual({ server: "srvA", id: 42, location: null });
  });
  it("is null for server and health alerts", () => {
    expect(alertEvent({ site_id: "srvA", detail: { skew_s: 4 } })).toBeNull();
    expect(alertEvent({ site_id: "srvA", detail: { name: "Gate", problems: ["no stream"] } })).toBeNull();
    expect(alertEvent({ site_id: "srvA", detail: { id: 0, camera_id: "cam1" } })).toBeNull();
  });
});

describe("cameraNameFor", () => {
  it("resolves per server, falling back to the id", () => {
    const names = new Map([["srvA/cam1", "Front door"], ["srvB/cam1", "Dock"]]);
    expect(cameraNameFor(names, "srvA")("cam1")).toBe("Front door");
    expect(cameraNameFor(names, "srvB")("cam1")).toBe("Dock");
    expect(cameraNameFor(names, "srvB")("cam9")).toBe("cam9");
  });
});

describe("activity pool", () => {
  it("keys events by server: the same id on two servers is two events", () => {
    expect(poolKey(ev("srvA", 1, "cam1", 0))).not.toBe(poolKey(ev("srvB", 1, "cam1", 0)));
  });
  it("applies a live update: replaces, sorts newest first, drops rejected and masked", () => {
    let pool = [ev("srvA", 1, "cam1", 100), ev("srvB", 1, "cam1", 50)];
    pool = applyLiveEvent(pool, ev("srvA", 2, "cam1", 200));
    expect(pool.map(poolKey)).toEqual(["srvA/2", "srvA/1", "srvB/1"]);
    pool = applyLiveEvent(pool, ev("srvA", 1, "cam1", 100, { synopsis: "new" }));
    expect(pool.find((e) => poolKey(e) === "srvA/1")?.synopsis).toBe("new");
    expect(pool).toHaveLength(3);
    expect(applyLiveEvent(pool, ev("srvB", 1, "cam1", 50, { status: "rejected" })).map(poolKey)).toEqual(["srvA/2", "srvA/1"]);
    expect(applyLiveEvent(pool, ev("srvA", 2, "cam1", 200, { status: "masked" })).map(poolKey)).toEqual(["srvA/1", "srvB/1"]);
    expect(removeLiveEvent(pool, "srvB", 1).map(poolKey)).toEqual(["srvA/2", "srvA/1"]);
  });
  it("merges the painted cameras' history under the recent list (recent wins)", () => {
    const scoped = [ev("srvA", 1, "cam1", 100, { synopsis: "old" }), ev("srvA", 3, "cam1", 10)];
    const recent = [ev("srvA", 1, "cam1", 100, { synopsis: "live" }), ev("srvB", 2, "cam2", 150)];
    const pool = mergePool(scoped, recent);
    expect(pool.map(poolKey)).toEqual(["srvB/2", "srvA/1", "srvA/3"]);
    expect(pool[1].synopsis).toBe("live");
  });
  it("tags a server's own events with the server", () => {
    expect(tagServer([{ id: 5, camera_id: "cam1" } as NvrEvent], "srvA", "Main")[0]).toMatchObject({ id: 5, site_id: "srvA", site_name: "Main" });
  });
});

describe("painted-region feed", () => {
  const region = bits([3, 4]);
  const crossed = encodeCells(bits([3, 4], [4, 4]));
  const elsewhere = encodeCells(bits([20, 10]));
  it("only counts this Site's servers' lane keys", () => {
    const map = { "srvA/cam1": region, "srvZ/cam1": region, cam1: region };
    expect(siteRegionKeys(map, ["srvA", "srvB"])).toEqual(["srvA/cam1"]);
    expect(siteRegionKeys({}, ["srvA"])).toEqual([]);
  });
  it("passes everything when nothing is painted", () => {
    const pool = [ev("srvA", 1, "cam1", 1)];
    expect(regionFeed(pool, {}, [])).toBe(pool);
  });
  it("keeps only the painted camera's events that crossed the region, open ones until they close", () => {
    const pool = [
      ev("srvA", 1, "cam1", 5, { cells: crossed }),
      ev("srvA", 2, "cam1", 4, { cells: elsewhere }),
      ev("srvA", 3, "cam1", 3, { status: "open", cells: null }),
      ev("srvB", 4, "cam1", 2, { cells: crossed }), // same camera id, other server: not painted
      ev("srvA", 5, "cam2", 1, { cells: crossed }), // other camera
      ev("srvA", 6, "cam1", 0, { cells: crossed, ptz_preset: "away" }), // camera was turned away
    ];
    const map = { "srvA/cam1": region };
    expect(regionFeed(pool, map, siteRegionKeys(map, ["srvA", "srvB"])).map((e) => e.id)).toEqual([1, 3]);
  });
});
