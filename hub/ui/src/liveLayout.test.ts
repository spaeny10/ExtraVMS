import { describe, expect, it } from "vitest";
import { EMPTY_LAYOUT, type LiveLayout, applyFocus, arrange, clampSpan, coerceLayout, defaultSpan, focusKey, freeCols, freeRowPx, gridCols, isDirty,
  isVisible, layoutCols, layoutIdStorageKey, layoutReducer, layoutStorageKey, parseLayout, shownKeys, shownSpan, spanAt, streamMode, tileLabel, tileQuality,
  tileMove, tileSpan, toSavedConfig, viewSummary, wrapIndex, type TileRect } from "./liveLayout";

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
    const l: LiveLayout = { ...EMPTY_LAYOUT, visible: ["a/1"], order: ["b/1", "a/1"], quality: { "a/1": "hd" }, cols: 2, activity: false };
    expect(parseLayout(JSON.stringify(l))).toEqual(l);
    expect(parseLayout(null)).toEqual(EMPTY_LAYOUT);
    expect(parseLayout("{nope")).toEqual(EMPTY_LAYOUT);
    expect(parseLayout(JSON.stringify({ visible: [1], order: "x", quality: { a: "4k", b: "sd" }, cols: 7, activity: "no" })))
      .toEqual({ ...EMPTY_LAYOUT, quality: { b: "sd" } });
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
  const saved: LiveLayout = { ...EMPTY_LAYOUT, visible: ["b/2", "a/1"], order: ["b/2", "a/1", "a/2", "b/1"], quality: { "b/2": "hd" }, cols: 2, activity: false };

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
      cols: 3, activity: true, mode: "auto", spans: {}, focus: null });
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
    expect(fromTimeline).toEqual({ ...EMPTY_LAYOUT, visible: ["a/1", "b/1"], order: ["b/1", "a/1"] });
    expect(shownKeys(all, fromTimeline)).toEqual(["b/1", "a/1"]);
    expect(isDirty(all, layoutReducer(EMPTY_LAYOUT, { type: "load", layout: fromTimeline }), fromTimeline)).toBe(false);
    // a Live layout the Timeline has since soloed a camera in: the solo isn't Live's, so the grid is unchanged
    expect(isDirty(all, saved, coerceLayout({ ...saved, solo: "a/1" }))).toBe(false);
    // and Live saves only its own fields (the hub keeps the solo beside them)
    expect(Object.keys(toSavedConfig(all, coerceLayout({ ...saved, solo: "a/1" }))).sort())
      .toEqual(["activity", "cols", "focus", "mode", "order", "quality", "spans", "visible"]);
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

describe("the View menu (streamMode, viewSummary)", () => {
  const keys = ["s/a", "s/b", "s/c"];
  const withQ = (quality: Record<string, "sd" | "hd">, extra: Partial<LiveLayout> = {}): LiveLayout => ({ ...EMPTY_LAYOUT, quality, ...extra });
  it("Stream is Auto, All SD, All HD or mixed over the live tiles", () => {
    expect(streamMode(EMPTY_LAYOUT, keys)).toBe("auto");
    expect(streamMode(withQ({ "s/a": "sd", "s/b": "sd", "s/c": "sd" }), keys)).toBe("sd");
    expect(streamMode(withQ({ "s/a": "hd", "s/b": "hd", "s/c": "hd" }), keys)).toBe("hd");
    expect(streamMode(withQ({ "s/a": "hd" }), keys)).toBe("mixed");
    // a pick on a tile that isn't live (offline server) doesn't count
    expect(streamMode(withQ({ "s/x": "hd" }), keys)).toBe("auto");
    expect(streamMode(withQ({ "s/a": "hd" }), [])).toBe("auto");
  });
  it("summarises columns and cameras for the button and its tooltip", () => {
    const all = viewSummary(EMPTY_LAYOUT, keys, 14, 14);
    expect(all).toMatchObject({ stream: "auto", cols: "Auto columns", cameras: "Cameras 14", filtered: null });
    expect(all.title).toBe("Stream Auto · Auto columns · all 14 cameras");
    const some = viewSummary(withQ({ "s/a": "sd", "s/b": "sd", "s/c": "sd" }, { cols: 3 }), keys, 9, 14);
    expect(some).toMatchObject({ stream: "sd", cols: "3 columns", cameras: "Cameras 9/14", filtered: "9/14" });
    expect(some.title).toBe("Stream All SD · 3 columns · 9/14 cameras shown");
    expect(viewSummary({ ...EMPTY_LAYOUT, cols: 1 }, keys, 1, 1).cols).toBe("1 column");
  });
});

describe("tileMove (arrow keys by position)", () => {
  // boxes of w x h at (x, y), keyed by name
  const box = (key: string, x: number, y: number, w: number, h: number): TileRect => ({ key, left: x, top: y, right: x + w, bottom: y + h });
  // Grid: 7 tiles, 3 wide (100 x 75, 8 px gaps):  t0 t1 t2 / t3 t4 t5 / t6
  const grid = Array.from({ length: 7 }, (_, i) => box(`t${i}`, (i % 3) * 108, Math.floor(i / 3) * 83, 100, 75));
  const mv = (from: number, key: string) => tileMove(grid, `t${from}`, key);

  it("Grid: left and right in reading order, stopping at the ends", () => {
    expect(mv(0, "ArrowLeft")).toBeNull();
    expect(mv(0, "ArrowRight")).toBe("t1");
    expect(mv(2, "ArrowRight")).toBe("t3");
    expect(mv(3, "ArrowLeft")).toBe("t2");
    expect(mv(6, "ArrowRight")).toBeNull();
  });
  it("Grid: up and down a column; a short last row's nearest tile, and nothing below it", () => {
    expect(mv(4, "ArrowUp")).toBe("t1");
    expect(mv(1, "ArrowUp")).toBeNull();
    expect(mv(0, "ArrowDown")).toBe("t3");
    expect(mv(5, "ArrowDown")).toBe("t6");
    expect(mv(6, "ArrowDown")).toBeNull();
  });
  it("Home, End, other keys and unknown tiles", () => {
    expect(mv(4, "Home")).toBe("t0");
    expect(mv(4, "End")).toBe("t6");
    expect(mv(0, "Home")).toBeNull();
    expect(mv(6, "End")).toBeNull();
    expect(mv(4, "Enter")).toBeNull();
    expect(tileMove(grid, "x", "ArrowRight")).toBeNull();
    expect(tileMove([], "x", "Home")).toBeNull();
  });
  it("Focus mode, strip beside: right from the large camera is the strip's first tile, left from any strip tile comes back", () => {
    const big = box("big", 0, 0, 800, 600);
    const strip = [0, 1, 2, 3].map((i) => box(`s${i}`, 808, i * 208, 228, 200));
    const all = [big, ...strip];
    expect(tileMove(all, "big", "ArrowRight")).toBe("s0");
    expect(tileMove(all, "s2", "ArrowLeft")).toBe("big");
    expect(tileMove(all, "s0", "ArrowDown")).toBe("s1");
    expect(tileMove(all, "s3", "ArrowLeft")).toBe("s2");  // below the large camera: reading order
    expect(tileMove(all, "big", "ArrowDown")).toBe("s3");
  });
  it("Freeform: mixed sizes go by what lines up", () => {
    // [ A 8x6 ][ B 4x3 ]
    //          [ C 4x3 ]
    // [ D 4x3 ][ E 4x3 ][ F 4x3 ]
    const tiles = [box("A", 0, 0, 800, 450), box("B", 808, 0, 392, 221), box("C", 808, 229, 392, 221),
      box("D", 0, 458, 392, 221), box("E", 400, 458, 400, 221), box("F", 808, 458, 392, 221)];
    expect(tileMove(tiles, "A", "ArrowRight")).toBe("B");
    expect(tileMove(tiles, "C", "ArrowLeft")).toBe("A");
    expect(tileMove(tiles, "A", "ArrowDown")).toBe("D");
    expect(tileMove(tiles, "E", "ArrowUp")).toBe("A");
    expect(tileMove(tiles, "B", "ArrowDown")).toBe("C");
    expect(tileMove(tiles, "C", "ArrowDown")).toBe("F");
  });
});

describe("modes: Freeform sizes and Focus", () => {
  const free: LiveLayout = { ...EMPTY_LAYOUT, mode: "free", spans: { "a/1": [8, 6], "gone/1": [2, 2] } };

  it("mode, span, resetSpans and focus actions; a no-op returns the same layout", () => {
    expect(layoutReducer(EMPTY_LAYOUT, { type: "mode", mode: "auto" })).toBe(EMPTY_LAYOUT);
    expect(layoutReducer(EMPTY_LAYOUT, { type: "mode", mode: "focus" }).mode).toBe("focus");
    expect(layoutReducer(free, { type: "span", key: "a/2", span: [6, 4] }).spans["a/2"]).toEqual([6, 4]);
    // clamped to the grid, whole cells
    expect(layoutReducer(free, { type: "span", key: "a/2", span: [20, 0.4] }).spans["a/2"]).toEqual([12, 1]);
    expect(layoutReducer(free, { type: "span", key: "a/1", span: [8, 6] })).toBe(free);
    expect(layoutReducer(free, { type: "span", key: "a/1", span: null }).spans).toEqual({ "gone/1": [2, 2] });
    expect(layoutReducer(free, { type: "span", key: "b/1", span: null })).toBe(free);
    expect(layoutReducer(free, { type: "resetSpans" }).spans).toEqual({});
    expect(layoutReducer(EMPTY_LAYOUT, { type: "resetSpans" })).toBe(EMPTY_LAYOUT);
    expect(layoutReducer(EMPTY_LAYOUT, { type: "focus", key: "b/1" }).focus).toBe("b/1");
  });

  it("default size: what the tile has in Grid mode", () => {
    expect(defaultSpan(EMPTY_LAYOUT, 9)).toEqual([4, 4]);
    expect(defaultSpan(EMPTY_LAYOUT, 1)).toEqual([12, 12]);
    expect(defaultSpan({ ...EMPTY_LAYOUT, cols: 2 }, 9)).toEqual([6, 6]);
    expect(tileSpan(free, "a/1", 9)).toEqual([8, 6]);
    expect(tileSpan(free, "a/2", 9)).toEqual([4, 4]);
    expect(clampSpan([0, 99])).toEqual([1, 12]);
  });

  it("narrow grids step down to 6 columns; spans halve, at least 2 wide, keeping their shape", () => {
    expect([0, 600, 899, 900, 1400].map(freeCols)).toEqual([12, 6, 6, 12, 12]);
    expect(shownSpan([8, 6], 12)).toEqual([8, 6]);
    expect(shownSpan([8, 6], 6)).toEqual([4, 3]);
    expect(shownSpan([12, 12], 6)).toEqual([6, 6]);
    expect(shownSpan([2, 2], 6)).toEqual([2, 2]);
    expect(shownSpan([1, 3], 6)).toEqual([2, 6]);
  });

  it("row height: a column's width at 16:9; a corner dragged snaps to whole cells", () => {
    // 1200 px, 12 columns, 8 px gaps: columns of 92.67 px, rows of 52.125 px
    expect(freeRowPx(1200, 12, 8)).toBeCloseTo(52.125);
    const col = (1200 - 88) / 12, row = col * 9 / 16;
    // a 4x4 tile is 4 columns + 3 gaps wide: dragged there it stays 4x4
    expect(spanAt(4 * col + 24, 4 * row + 24, col, row, 8)).toEqual([4, 4]);
    expect(spanAt(4.6 * col + 24, 2.2 * row, col, row, 8)).toEqual([5, 2]);
    expect(spanAt(-50, -50, col, row, 8)).toEqual([1, 1]);
    expect(spanAt(99999, 99999, col, row, 8, 6)).toEqual([6, 12]);
  });

  it("Focus mode's camera: the pick while shown, else the first", () => {
    expect(focusKey(all, EMPTY_LAYOUT)).toBe("a/1");
    expect(focusKey(all, { ...EMPTY_LAYOUT, focus: "b/1" })).toBe("b/1");
    expect(focusKey(["a/1"], { ...EMPTY_LAYOUT, focus: "b/1" })).toBe("a/1");
    expect(focusKey([], EMPTY_LAYOUT)).toBe(null);
  });

  it("dirty: mode, the shown tiles' sizes, and the large camera in Focus mode only", () => {
    expect(isDirty(all, free, free)).toBe(false);
    expect(isDirty(all, { ...free, mode: "focus" }, free)).toBe(true);
    expect(isDirty(all, layoutReducer(free, { type: "span", key: "b/2", span: [3, 3] }), free)).toBe(true);
    // a size kept for a camera the Site no longer has, or a hidden one, doesn't show
    expect(isDirty(all, { ...free, spans: { "a/1": [8, 6] } }, free)).toBe(false);
    expect(isDirty(all, { ...free, focus: "b/1" }, free)).toBe(false);
    const focus: LiveLayout = { ...EMPTY_LAYOUT, mode: "focus" };
    expect(isDirty(all, { ...focus, focus: "b/1" }, focus)).toBe(true);
    // naming the camera that is large anyway is no change
    expect(isDirty(all, { ...focus, focus: "a/1" }, focus)).toBe(false);
  });

  it("read tolerantly, saved for the Site's cameras only", () => {
    expect(coerceLayout({ mode: "free", spans: { "a/1": [8, 6], "a/2": [13, 1], "b/1": [4], "b/2": ["4", 4] }, focus: "a/2" }))
      .toEqual({ ...EMPTY_LAYOUT, mode: "free", spans: { "a/1": [8, 6] }, focus: "a/2" });
    expect(coerceLayout({ mode: "grid", spans: [1, 2], focus: 3 })).toEqual(EMPTY_LAYOUT);
    const s = toSavedConfig(all, { ...free, focus: "gone/1" });
    expect(s.spans).toEqual({ "a/1": [8, 6] });
    expect(s.focus).toBe(null);
    expect(parseLayout(JSON.stringify(free))).toEqual({ ...free, spans: { "a/1": [8, 6], "gone/1": [2, 2] } });
  });
});
