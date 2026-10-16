import { describe, expect, it } from "vitest";
import { EMPTY_LAYOUT, type LiveLayout, applyFocus, arrange, coerceLayout, gridCols, isDirty, isVisible, layoutCols, layoutIdStorageKey, layoutReducer,
  layoutStorageKey, parseLayout, shownKeys, tileLabel, tileQuality, toSavedConfig, wrapIndex } from "./liveLayout";

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
  it("Auto forgets the pick: a tile without one is on Auto", () => {
    const two = layoutReducer(EMPTY_LAYOUT, { type: "allQuality", keys: ["a/1", "b/1"], quality: "hd" });
    const one = layoutReducer(two, { type: "quality", key: "a/1", quality: "auto" });
    expect(one.quality).toEqual({ "b/1": "hd" });
    expect([tileQuality(one, "a/1"), tileQuality(one, "b/1")]).toEqual(["auto", "hd"]);
    expect(layoutReducer(one, { type: "allQuality", keys: ["a/1", "b/1"], quality: "auto" }).quality).toEqual({});
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
    const l: LiveLayout = { visible: ["a/1"], order: ["b/1", "a/1"], quality: { "a/1": "hd" }, cols: 2, activity: false };
    expect(parseLayout(JSON.stringify(l))).toEqual(l);
    expect(parseLayout(null)).toEqual(EMPTY_LAYOUT);
    expect(parseLayout("{nope")).toEqual(EMPTY_LAYOUT);
    expect(parseLayout(JSON.stringify({ visible: [1], order: "x", quality: { a: "4k", b: "sd" }, cols: 7, activity: "no" })))
      .toEqual({ visible: null, order: null, quality: { b: "sd" }, cols: null, activity: true });
  });
  it("an arrangement stored before layouts had columns and the activity column reads as Auto, shown", () => {
    expect(parseLayout(JSON.stringify({ visible: null, order: ["b/1"], quality: {} }))).toEqual({ ...EMPTY_LAYOUT, order: ["b/1"] });
  });
  it("a hub layout's config read tolerantly", () => {
    expect(coerceLayout(null)).toEqual(EMPTY_LAYOUT);
    expect(coerceLayout(["a/1"])).toEqual(EMPTY_LAYOUT);
    expect(coerceLayout({ visible: ["a/1"], cols: 3, solo: "a/1" })).toEqual({ ...EMPTY_LAYOUT, visible: ["a/1"], cols: 3 });
  });
  it("storage key per site", () => {
    expect(layoutStorageKey("l_1")).toBe("siteLive.l_1");
    expect(layoutIdStorageKey("l_1")).toBe("siteLive.l_1.layoutId");
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

describe("named layouts", () => {
  // a saved layout: b/2 then a/1 shown, a/2 hidden, b/2 in HD, 2 columns, no activity column
  const saved: LiveLayout = { visible: ["b/2", "a/1"], order: ["b/2", "a/1", "a/2", "b/1"], quality: { "b/2": "hd" }, cols: 2, activity: false };

  it("applied to the Site's cameras: its cameras in its order, new cameras left out, gone ones dropped", () => {
    expect(shownKeys(all, saved)).toEqual(["b/2", "a/1"]);
    // a camera added to the Site since (c/1) stays out; one removed (b/2) just disappears
    expect(shownKeys(["a/1", "a/2", "b/1", "c/1"], saved)).toEqual(["a/1"]);
    // "All cameras" shows everything, new ones included
    expect(shownKeys([...all, "c/1"], EMPTY_LAYOUT)).toEqual([...all, "c/1"]);
  });

  it("saved spelled out: explicit cameras and order of the Site's current cameras, quality only for those", () => {
    const working: LiveLayout = { ...EMPTY_LAYOUT, visible: null, order: ["gone/1", "b/1"], quality: { "a/1": "hd", "gone/1": "hd" }, cols: 3 };
    expect(toSavedConfig(all, working)).toEqual({ visible: ["b/1", "a/1", "a/2", "b/2"], order: ["b/1", "a/1", "a/2", "b/2"], quality: { "a/1": "hd" },
      cols: 3, activity: true });
    // ...so a camera added later doesn't join it
    expect(shownKeys([...all, "c/1"], toSavedConfig(all, working))).not.toContain("c/1");
  });

  it("dirty: only what the viewer sees counts", () => {
    expect(isDirty(all, saved, saved)).toBe(false);
    // showing every camera again (visible back to null) while the layout lists all of them: the same grid
    const allListed: LiveLayout = { ...saved, visible: [...all] };
    expect(isDirty(all, { ...allListed, visible: null }, allListed)).toBe(false);
    // the order of hidden cameras doesn't show
    expect(isDirty(all, { ...saved, order: ["b/2", "a/1", "b/1", "a/2"] }, saved)).toBe(false);
    // a camera the Site no longer has doesn't make it dirty
    expect(isDirty(["a/1", "a/2", "b/1"], { ...saved, visible: ["a/1"] }, saved)).toBe(false);
    // a tile set back to Auto (its default) doesn't either; an SD pick does (SD is a choice now, not the default)
    expect(isDirty(all, layoutReducer(saved, { type: "quality", key: "a/1", quality: "auto" }), saved)).toBe(false);
    expect(isDirty(all, { ...saved, quality: { "b/2": "hd", "a/1": "sd" } }, saved)).toBe(true);
    for (const a of [
      { type: "toggle", key: "a/2", all },
      { type: "drop", key: "a/1", onto: "b/2", all },
      { type: "quality", key: "a/1", quality: "hd" },
      { type: "cols", cols: null },
      { type: "activity", on: true },
    ] as const) expect(isDirty(all, layoutReducer(saved, a), saved), a.type).toBe(true);
  });

  it("dirty under All cameras: the activity column is the viewer's preference, not a change", () => {
    expect(isDirty(all, { ...EMPTY_LAYOUT, activity: false }, EMPTY_LAYOUT, false)).toBe(false);
    expect(isDirty(all, { ...EMPTY_LAYOUT, cols: 1 }, EMPTY_LAYOUT, false)).toBe(true);
    expect(isDirty(all, layoutReducer(EMPTY_LAYOUT, { type: "toggle", key: "a/1", all }), EMPTY_LAYOUT, false)).toBe(true);
  });

  it("shared with the Timeline: its fields are read as Live's defaults and never count as a change", () => {
    // saved on the Timeline: {visible, solo, order} only; Live reads it with SD streams, Auto columns, activity shown
    const fromTimeline = coerceLayout({ visible: ["a/1", "b/1"], solo: "b/1", order: ["b/1", "a/1"] });
    expect(fromTimeline).toEqual({ visible: ["a/1", "b/1"], order: ["b/1", "a/1"], quality: {}, cols: null, activity: true });
    expect(shownKeys(all, fromTimeline)).toEqual(["b/1", "a/1"]);
    expect(isDirty(all, layoutReducer(EMPTY_LAYOUT, { type: "load", layout: fromTimeline }), fromTimeline)).toBe(false);
    // a Live layout the Timeline has since soloed a camera in: the solo isn't Live's, so the grid is unchanged
    expect(isDirty(all, saved, coerceLayout({ ...saved, solo: "a/1" }))).toBe(false);
    // and Live saves only its own fields (the hub keeps the solo beside them)
    expect(Object.keys(toSavedConfig(all, coerceLayout({ ...saved, solo: "a/1" }))).sort()).toEqual(["activity", "cols", "order", "quality", "visible"]);
  });

  it("load replaces the arrangement; cols and activity", () => {
    expect(layoutReducer(EMPTY_LAYOUT, { type: "load", layout: saved })).toBe(saved);
    expect(layoutReducer(saved, { type: "cols", cols: 2 })).toBe(saved);
    expect(layoutReducer(saved, { type: "cols", cols: 4 }).cols).toBe(4);
    expect(layoutReducer(saved, { type: "activity", on: true }).activity).toBe(true);
    expect(layoutCols(saved, 9)).toBe(2);
    expect(layoutCols(EMPTY_LAYOUT, 9)).toBe(3);
  });
});

describe("drop (tile drag)", () => {
  it("the dragged tile takes the target's place, in either direction", () => {
    const fwd = layoutReducer(EMPTY_LAYOUT, { type: "drop", key: "a/1", onto: "b/1", all });
    expect(arrange(all, fwd)).toEqual(["a/2", "b/1", "a/1", "b/2"]);
    const back = layoutReducer(EMPTY_LAYOUT, { type: "drop", key: "b/2", onto: "a/2", all });
    expect(arrange(all, back)).toEqual(["a/1", "b/2", "a/2", "b/1"]);
  });
  it("past a hidden camera, the shown order is what the drop asked for", () => {
    const hid = layoutReducer(EMPTY_LAYOUT, { type: "toggle", key: "a/2", all });
    expect(shownKeys(all, layoutReducer(hid, { type: "drop", key: "a/1", onto: "b/1", all }))).toEqual(["b/1", "a/1", "b/2"]);
  });
  it("onto itself or an unknown camera changes nothing", () => {
    expect(layoutReducer(EMPTY_LAYOUT, { type: "drop", key: "a/1", onto: "a/1", all })).toBe(EMPTY_LAYOUT);
    expect(layoutReducer(EMPTY_LAYOUT, { type: "drop", key: "a/1", onto: "x/9", all })).toBe(EMPTY_LAYOUT);
    expect(layoutReducer(EMPTY_LAYOUT, { type: "drop", key: "x/9", onto: "a/1", all })).toBe(EMPTY_LAYOUT);
  });
  it("in a saved layout whose cameras have changed, gone ones drop out of the new order", () => {
    const l: LiveLayout = { ...EMPTY_LAYOUT, order: ["gone/1", "b/2", "a/1"] };
    expect(layoutReducer(l, { type: "drop", key: "a/1", onto: "b/2", all }).order).toEqual(["a/1", "b/2", "a/2", "b/1"]);
  });
});
