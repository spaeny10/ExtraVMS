import { describe, expect, it } from "vitest";
import { EMPTY_LAYOUT, applyFocus, arrange, gridCols, isVisible, layoutReducer, layoutStorageKey, parseLayout, tileLabel, wrapIndex } from "./liveLayout";

describe("gridCols", () => {
  it("is square-ish and at most 4 wide, like the server's LiveView", () => {
    expect([0, 1, 2, 4, 5, 9, 10, 16, 17, 40].map(gridCols)).toEqual([1, 1, 2, 2, 3, 3, 4, 4, 4, 4]);
  });
});

describe("tileLabel", () => {
  it("names the server only when the site has several", () => {
    expect(tileLabel("Hailo T1", "Gate", true)).toBe("Hailo T1 · Gate");
    expect(tileLabel("Hailo T1", "Gate", false)).toBe("Gate");
  });
});

describe("wrapIndex", () => {
  it("wraps across the ends", () => {
    expect(wrapIndex(0, -1, 5)).toBe(4);
    expect(wrapIndex(4, 1, 5)).toBe(0);
    expect(wrapIndex(2, 1, 5)).toBe(3);
    expect(wrapIndex(0, 1, 0)).toBe(0);
  });
});

const all = ["a/1", "a/2", "b/1", "b/2"];

describe("layoutReducer", () => {
  it("hides and shows; all shown again goes back to null", () => {
    const hidden = layoutReducer(EMPTY_LAYOUT, { type: "toggle", key: "a/2", all });
    expect(hidden.visible).toEqual(["a/1", "b/1", "b/2"]);
    expect(isVisible(hidden, "a/2")).toBe(false);
    expect(isVisible(hidden, "b/1")).toBe(true);
    expect(layoutReducer(hidden, { type: "toggle", key: "a/2", all }).visible).toBeNull();
  });
  it("quality per camera and for all", () => {
    const one = layoutReducer(EMPTY_LAYOUT, { type: "quality", key: "b/1", quality: "hd" });
    expect(one.quality).toEqual({ "b/1": "hd" });
    expect(layoutReducer(one, { type: "allQuality", keys: ["a/1", "b/1"], quality: "sd" }).quality).toEqual({ "a/1": "sd", "b/1": "sd" });
  });
  it("moves a camera before another, or to the end", () => {
    const m = layoutReducer(EMPTY_LAYOUT, { type: "move", key: "b/2", before: "a/1", all });
    expect(arrange(all, m)).toEqual(["b/2", "a/1", "a/2", "b/1"]);
    const end = layoutReducer(m, { type: "move", key: "a/1", before: null, all });
    expect(arrange(all, end)).toEqual(["b/2", "a/2", "b/1", "a/1"]);
    expect(layoutReducer(m, { type: "move", key: "a/1", before: "a/1", all })).toBe(m);
  });
  it("reset", () => {
    const m = layoutReducer(EMPTY_LAYOUT, { type: "toggle", key: "a/1", all });
    expect(layoutReducer(m, { type: "reset" })).toEqual(EMPTY_LAYOUT);
  });
});

describe("arrange", () => {
  it("drops unknown keys and appends new cameras in default order", () => {
    expect(arrange(all, { ...EMPTY_LAYOUT, order: ["gone/9", "b/1"] })).toEqual(["b/1", "a/1", "a/2", "b/2"]);
    expect(arrange(all, EMPTY_LAYOUT)).toBe(all);
  });
});

describe("parseLayout", () => {
  it("round-trips and tolerates junk", () => {
    const l = { visible: ["a/1"], order: ["b/1", "a/1"], quality: { "a/1": "hd" as const } };
    expect(parseLayout(JSON.stringify(l))).toEqual(l);
    expect(parseLayout(null)).toEqual(EMPTY_LAYOUT);
    expect(parseLayout("{nope")).toEqual(EMPTY_LAYOUT);
    expect(parseLayout(JSON.stringify({ visible: [1], order: "x", quality: { a: "4k", b: "sd" } }))).toEqual({ visible: null, order: null, quality: { b: "sd" } });
  });
  it("storage key per site", () => {
    expect(layoutStorageKey("l_1")).toBe("siteLive.l_1");
  });
});

describe("applyFocus", () => {
  it("first: focused cameras lead in focus order, the rest follow", () => {
    expect(applyFocus(all, { keys: ["b/2", "a/2"], mode: "first" })).toEqual(["b/2", "a/2", "a/1", "b/1"]);
  });
  it("only: just the focused cameras", () => {
    expect(applyFocus(all, { keys: ["b/1", "a/1"], mode: "only" })).toEqual(["b/1", "a/1"]);
  });
  it("drops unknown and duplicate keys", () => {
    expect(applyFocus(all, { keys: ["x/9", "a/2", "a/2"], mode: "only" })).toEqual(["a/2"]);
  });
  it("no focus, or none of it known, leaves the order alone", () => {
    expect(applyFocus(all, null)).toBe(all);
    expect(applyFocus(all, undefined)).toBe(all);
    expect(applyFocus(all, { keys: [], mode: "only" })).toBe(all);
    expect(applyFocus(all, { keys: ["x/9"], mode: "only" })).toBe(all);
  });
});
