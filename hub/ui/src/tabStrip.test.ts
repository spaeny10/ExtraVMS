import { describe, expect, it } from "vitest";
import { centreTab, edgeFade, fadeClass } from "./tabStrip";

describe("edgeFade", () => {
  it("no fade when everything fits", () => {
    expect(edgeFade(0, 355, 355)).toEqual({ left: false, right: false });
    expect(fadeClass(edgeFade(0, 355, 355.6))).toBe("");   // sub-pixel overflow is not overflow
  });
  it("fades the edge(s) that hide tabs", () => {
    expect(edgeFade(0, 355, 520)).toEqual({ left: false, right: true });
    expect(edgeFade(80, 355, 520)).toEqual({ left: true, right: true });
    expect(edgeFade(165, 355, 520)).toEqual({ left: true, right: false });
    expect(fadeClass(edgeFade(80, 355, 520))).toBe("fade-l fade-r");
  });
});

describe("centreTab", () => {
  it("centers a tab past the right edge", () => {
    // strip 355 wide, 520 of tabs; a 90 px tab 400 px from the visible left edge
    expect(centreTab(0, 355, 520, 400, 90)).toBe(165);       // clamped: can't scroll past the end
    expect(centreTab(0, 355, 520, 250, 80)).toBe(113);
  });
  it("never scrolls before the start, and accounts for the current scroll", () => {
    expect(centreTab(100, 355, 520, -90, 60)).toBe(0);
    expect(centreTab(100, 355, 520, 50, 60)).toBe(3);
  });
});
