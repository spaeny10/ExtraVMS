import { describe, expect, it } from "vitest";
import { defaultVisible, effectiveOffset, focusFor, fromLaneKeys, initialTimelineLayout, journeyMembers, layoutsStorageKey, lightSolo, localLayoutStore,
  momentTarget, parseLayouts, parseSiteTimelineQuery, siteTimelineHref, withoutTimelineParams } from "./timelineLink";

describe("siteTimelineHref", () => {
  it("links an event, a journey and a moment", () => {
    expect(siteTimelineHref("loc1", "srvA", "cam1", 42)).toBe("/sites/loc1/timeline?server=srvA&cam=cam1&event=42");
    expect(siteTimelineHref("loc1", "srvA", "cam1", 42, null, true)).toBe("/sites/loc1/timeline?server=srvA&cam=cam1&event=42&journey=1");
    expect(siteTimelineHref("loc1", "srvA", "cam1", null, 1790270080.6)).toBe("/sites/loc1/timeline?server=srvA&cam=cam1&t=1790270081");
  });
  it("round-trips through the parser", () => {
    const href = siteTimelineHref("loc1", "srv A", "cam_2", 7, null, true);
    expect(parseSiteTimelineQuery(href.split("?")[1])).toEqual({ server: "srv A", cam: "cam_2", event: 7, t: null, journey: true, region: null });
  });
});

describe("parseSiteTimelineQuery", () => {
  it("reads the query string, with or without ?, or URLSearchParams", () => {
    const want = { server: "s1", cam: "c1", event: null, t: 1790270080, journey: false, region: null };
    expect(parseSiteTimelineQuery("?server=s1&cam=c1&t=1790270080")).toEqual(want);
    expect(parseSiteTimelineQuery("server=s1&cam=c1&t=1790270080")).toEqual(want);
    expect(parseSiteTimelineQuery(new URLSearchParams("server=s1&cam=c1&t=1790270080"))).toEqual(want);
  });
  it("ignores junk numbers and needs a camera", () => {
    expect(parseSiteTimelineQuery("?server=s1&cam=c1&event=abc&t=-5")).toMatchObject({ event: null, t: null });
    expect(parseSiteTimelineQuery("?server=s1&event=5")).toBeNull();
    expect(parseSiteTimelineQuery("")).toBeNull();
  });
  it("falls back to the Timeline's own #timeline share link", () => {
    expect(parseSiteTimelineQuery("", "#timeline?cam=c1&event=9&journey=1&server=s2"))
      .toEqual({ server: "s2", cam: "c1", event: 9, t: null, journey: true, region: null });
    expect(parseSiteTimelineQuery("?server=s1&cam=c1&event=3", "#timeline?cam=c9&event=9")).toMatchObject({ cam: "c1", event: 3 });
  });
  it("reads a painted region", () => {
    const cells = "A".repeat(96);
    expect(parseSiteTimelineQuery(`?server=s1&cam=c1&t=5&region=c1:${cells}`)?.region).toEqual({ cam: "c1", cells });
  });
});

describe("withoutTimelineParams", () => {
  it("drops the link parameters and keeps the rest", () => {
    expect(withoutTimelineParams("?server=s&cam=c&event=1&journey=1&x=2")).toBe("?x=2");
    expect(withoutTimelineParams("?server=s&cam=c&t=5")).toBe("");
  });
});

describe("focusFor", () => {
  it("uses lane keys and the event's span, like the server UI", () => {
    const f = focusFor("srvA", { id: 5, camera_id: "cam1", start_ts: 100, end_ts: 130, camera_class: "person" }, 0, 1);
    expect(f).toEqual({ eventId: 5, cam: "srvA/cam1", start: 100, end: 130, label: "person", nonce: 1, members: undefined });
  });
  it("covers every sighting of a journey", () => {
    const members = journeyMembers([{ id: 1, camera_id: "a", start_ts: 100, end_ts: 110 }, { id: 2, camera_id: "b", start_ts: 120, end_ts: null }]);
    const f = focusFor("s", { id: 1, camera_id: "a", start_ts: 100, end_ts: 110, members }, 0, 1);
    expect(f.cam).toBe("s/a");
    expect(f.members).toEqual([{ id: 1, cam: "s/a", start: 100, end: 110 }, { id: 2, cam: "s/b", start: 120, end: 120 }]);
    expect([f.start, f.end]).toEqual([100, 120]);
    expect(f.label).toBe("journey across 2 cameras");
  });
  it("moves server times onto the shared clock", () => {
    const f = focusFor("s", momentTarget("c", 1000), 10, 1);
    expect([f.eventId, f.start, f.end, f.label]).toEqual([0, 990, 995, "moment"]);
  });
});

describe("effectiveOffset", () => {
  it("ignores skew up to 2 s", () => {
    expect([effectiveOffset(null), effectiveOffset(1.5), effectiveOffset(-2), effectiveOffset(3), effectiveOffset(-30)]).toEqual([0, 0, 0, 3, -30]);
  });
});

describe("defaultVisible", () => {
  const cams = (server: string, n: number) => Array.from({ length: n }, (_, i) => ({ key: `${server}/c${i}`, server }));
  it("shows everything at a small site", () => {
    expect(defaultVisible([...cams("a", 4), ...cams("b", 4)])).toBeNull();
  });
  it("shows the first 8 of each server at a big one", () => {
    const v = defaultVisible([...cams("a", 12), ...cams("b", 3)])!;
    expect(v).toHaveLength(11);
    expect(v).toContain("a/c7");
    expect(v).not.toContain("a/c8");
    expect(v).toContain("b/c2");
  });
  it("is null when the cap hides nothing", () => {
    expect(defaultVisible([...cams("a", 6), ...cams("b", 6)])).toBeNull();
  });
});

describe("localLayoutStore", () => {
  const memory = () => { const m = new Map<string, string>(); return { getItem: (k: string) => m.get(k) ?? null, setItem: (k: string, v: string) => void m.set(k, v), m }; };
  it("creates, lists by name, updates and removes per site", async () => {
    const kv = memory();
    const s = localLayoutStore("loc1", kv, () => 1000);
    const a = await s.create({ name: "Night", config: { visible: ["x/1"], solo: null } });
    const b = await s.create({ name: "Doors", config: { visible: null, solo: "x/2" } });
    expect([a.id, b.id]).toEqual([1, 2]);
    expect((await s.list()).map((l) => l.name)).toEqual(["Doors", "Night"]);
    const u = await s.update(1, { name: "Nights", config: { visible: ["x/3"], solo: null } });
    expect(u).toMatchObject({ id: 1, name: "Nights", created_at: 1000 });
    await s.remove(2);
    expect((await s.list()).map((l) => l.id)).toEqual([1]);
    expect(kv.m.has(layoutsStorageKey("loc1"))).toBe(true);
    expect(await localLayoutStore("loc2", kv).list()).toEqual([]);
    await expect(s.update(99, { name: "x", config: { visible: null, solo: null } })).rejects.toThrow();
  });
  it("survives junk in storage", () => {
    expect(parseLayouts("not json")).toEqual([]);
    expect(parseLayouts('[{"id":1,"name":"ok","config":{}},{"id":"2"}]')).toHaveLength(1);
  });
});

describe("fromLaneKeys", () => {
  it("reads the server from the lane key and returns the server's own camera ids", () => {
    const e = { id: 5, camera_id: "srvA/cam1", start_ts: 100, end_ts: 130 };
    expect(fromLaneKeys(e)).toEqual({ server: "srvA", target: { ...e, camera_id: "cam1" } });
    const j = { ...e, members: [{ id: 5, cam: "srvA/cam1", start: 100, end: 130 }, { id: 6, cam: "srvA/cam/2", start: 140, end: 150 }] };
    expect(fromLaneKeys(j)?.target.members).toEqual([{ id: 5, cam: "cam1", start: 100, end: 130 }, { id: 6, cam: "cam/2", start: 140, end: 150 }]);
  });
  it("is null for a plain camera id (no server in the key)", () => {
    expect(fromLaneKeys({ id: 5, camera_id: "cam1", start_ts: 100, end_ts: 130 })).toBeNull();
  });
});

describe("lighter remote default", () => {
  const cams = (n: number) => Array.from({ length: n }, (_, i) => ({ key: `srv/c${i + 1}`, server: "srv" }));
  const keys = (n: number) => cams(n).map((c) => c.key);
  it("solos one camera only via the hub and only past two cameras", () => {
    expect(lightSolo(keys(3), null, null, true)).toBe("srv/c1");
    expect(lightSolo(keys(3), null, "srv/c2", true)).toBe("srv/c2");
    expect(lightSolo(keys(3), ["srv/c3"], null, true)).toBe("srv/c3");
    expect(lightSolo(keys(3), null, "gone/c9", true)).toBe("srv/c1");
    expect(lightSolo(keys(2), null, null, true)).toBeNull();
    expect(lightSolo(keys(5), null, null, false)).toBeNull();
  });
  it("first visit via the hub stores the solo and flags it; direct keeps the grid", () => {
    expect(initialTimelineLayout({ stored: null, flag: null, cams: cams(3), focusKey: "srv/c2", viaHub: true }))
      .toEqual({ config: { visible: null, solo: "srv/c2", order: null }, flag: "srv/c2" });
    expect(initialTimelineLayout({ stored: null, flag: null, cams: cams(3), focusKey: null, viaHub: false })).toEqual({});
  });
  it("undoes its own solo once direct, and drops the flag once the user changed the layout", () => {
    const stored = { visible: null, solo: "srv/c1", order: null };
    expect(initialTimelineLayout({ stored, flag: "srv/c1", cams: cams(3), focusKey: null, viaHub: false }))
      .toEqual({ config: { ...stored, solo: null }, flag: null });
    expect(initialTimelineLayout({ stored, flag: "srv/c1", cams: cams(3), focusKey: null, viaHub: true })).toEqual({});
    expect(initialTimelineLayout({ stored: { ...stored, solo: null }, flag: "srv/c1", cams: cams(3), focusKey: null, viaHub: false })).toEqual({ flag: null });
    // a saved layout (no flag) is never touched
    expect(initialTimelineLayout({ stored: { ...stored, solo: null }, flag: null, cams: cams(3), focusKey: null, viaHub: true })).toEqual({});
  });
});
