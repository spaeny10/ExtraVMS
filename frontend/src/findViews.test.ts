import { expect, test } from "vitest";
import type { NvrEvent } from "./api";
import {
  DEFAULT_FILTERS, PRESETS, activeFilterCount, buildQuery, defaultViewId, eventQuery, fromSaved, hourGroups, matchesFilters, parseQuery,
  placeNames, resolveFilters, sameFilters, toSaved, type FindFilters,
} from "./findViews";

const view = (id: string) => PRESETS.find((v) => v.id === id)!;

test("URL round trip: only differences from the view are written, and read back", () => {
  const base = resolveFilters(view("attention"), 0.3);
  expect(buildQuery("attention", base, base, "", "events")).toBe("view=attention");
  const f: FindFilters = { ...base, camera: "cam3", flags: ["unusual", "ppe"], priority: "medium", place: "Side Yard", status: "", hours: 0 };
  const qs = buildQuery("attention", f, base, "red truck", "grouped");
  expect(qs).toContain("flags=ppe%2Cunusual");
  const back = parseQuery("?" + qs);
  expect(back.view).toBe("attention");
  expect(back.q).toBe("red truck");
  expect(back.mode).toBe("grouped");
  expect(sameFilters({ ...base, ...back.patch }, f)).toBe(true);
});

test("URL parsing ignores junk and unknown flags", () => {
  const s = parseQuery("view=x&flags=ppe,bogus&priority=urgent&hours=-3&yolo=7&type=cat&day=yesterday&sort=up&zzz=1");
  expect(s.patch).toEqual({ flags: ["ppe"] });
  expect(parseQuery("").view).toBeUndefined();
  expect(parseQuery("flags=none").patch.flags).toEqual([]);
  expect(parseQuery("priority=any&status=all").patch).toEqual({ priority: "", status: "" });
});

test("flags compare without order", () => {
  const a = { ...DEFAULT_FILTERS, flags: ["ppe", "rule"] } as FindFilters;
  const b = { ...DEFAULT_FILTERS, flags: ["rule", "ppe"] } as FindFilters;
  expect(sameFilters(a, b)).toBe(true);
  expect(sameFilters(a, { ...b, flags: ["rule"] })).toBe(false);
});

test("default view falls back to Attention", () => {
  expect(defaultViewId(PRESETS, null)).toBe("attention");
  expect(defaultViewId(PRESETS, "gone")).toBe("attention");
  expect(defaultViewId(PRESETS, "compliance")).toBe("compliance");
  const saved = fromSaved({ id: "s1", name: "PPE this week", filters: { flags: ["ppe", "nope"], hours: "x" } });
  expect(saved.filters).toEqual({ flags: ["ppe"] });
  expect(defaultViewId([...PRESETS, saved], "s1")).toBe("s1");
});

test("saved views keep the viewer's own confidence slider", () => {
  const f = { ...DEFAULT_FILTERS, minYolo: 0.6, flags: ["ppe"] } as FindFilters;
  const s = toSaved("s1", "PPE", f, "events");
  expect("minYolo" in s.filters).toBe(false);
  expect(resolveFilters(fromSaved(s), 0.4).minYolo).toBe(0.4);
});

test("live events are checked with the backend's rules", () => {
  const now = Date.now();
  const e = { id: 1, camera_id: "cam1", camera_class: "person", status: "verified", start_ts: now / 1000 - 60, yolo_conf: 0.8,
    priority: "medium", policy: { kind: "ppe", text: "x", priority: "medium" }, areas: [{ name: "Yard", from: 0, to: 1 }] } as unknown as NvrEvent;
  const f = resolveFilters(view("attention"), 0);
  expect(matchesFilters(e, f, now)).toBe(true);
  expect(matchesFilters(e, { ...f, flags: ["ppe"], place: "Yard", priority: "medium" }, now)).toBe(true);
  expect(matchesFilters(e, { ...f, priority: "high" }, now)).toBe(false);
  expect(matchesFilters(e, { ...f, flags: ["watched"] }, now)).toBe(false);
  expect(matchesFilters({ ...e, priority: "none", policy: null }, f, now)).toBe(false);
  expect(eventQuery({ ...f, flags: ["ppe"] }, now)).toMatchObject({ attention: true, flags: ["ppe"], status: "verified" });
});

test("places are the cameras' named areas", () => {
  const cams = [
    { id: "a", zones: [{ name: "Yard", type: "area", points: [] }, { name: "Side Yard", type: "ppe", points: [] }] },
    { id: "b", zones: [{ name: "Gate", type: "area", points: [] }, { name: "Yard", type: "area", points: [] }] },
  ] as never;
  expect(placeNames(cams)).toEqual(["Gate", "Yard"]);
  expect(placeNames(cams, "a")).toEqual(["Yard"]);
});

test("hour headers group consecutive events by local hour", () => {
  const now = new Date(2026, 9, 1, 16, 30);
  const t = (h: number, m: number) => new Date(2026, 9, 1, h, m).getTime() / 1000;
  const g = hourGroups([{ start_ts: t(15, 50) }, { start_ts: t(15, 5) }, { start_ts: t(14, 59) }], now);
  expect(g.map((x) => x.events.length)).toEqual([2, 1]);
  expect(g[0].label.startsWith("Today")).toBe(true);
});

test("activeFilterCount: what the phone's Filters toggle counts", () => {
  expect(activeFilterCount(DEFAULT_FILTERS)).toBe(0);
  // Attention: needs attention + most important first (24 h is the default window)
  expect(activeFilterCount(resolveFilters(view("attention")))).toBe(2);
  // Investigate: any time
  expect(activeFilterCount(resolveFilters(view("investigate")))).toBe(1);
  const f: FindFilters = { ...DEFAULT_FILTERS, camera: "cam3", flags: ["ppe", "rule"], day: "2026-10-01", minYolo: 0.4, status: "", sort: "priority" };
  expect(activeFilterCount(f)).toBe(7);
  // searching: status and sort have no say (their controls are hidden)
  expect(activeFilterCount(f, false)).toBe(5);
  // the slider set back to 0 and the default window are not filters
  expect(activeFilterCount({ ...DEFAULT_FILTERS, minYolo: 0, hours: 24 })).toBe(0);
});
