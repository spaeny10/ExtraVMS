import { describe, expect, it } from "vitest";
import { wheelAction } from "./VideoZoom";

const wheel = (deltaY: number, o: { ctrlKey?: boolean; deltaX?: number } = {}) => ({ ctrlKey: false, deltaX: 0, deltaY, ...o });

describe("wheelAction (a wheel over a video tile)", () => {
  it("at 1× a plain wheel scrolls the page while it can, up and down", () => {
    expect(wheelAction(false, wheel(-100), true)).toBe("page");
    expect(wheelAction(false, wheel(100), true)).toBe("page");
    expect(wheelAction(false, wheel(100))).toBe("page");                 // nothing to zoom out of at 1×
    expect(wheelAction(false, wheel(0, { deltaX: 50 }))).toBe("page");   // sideways swipe: nothing to pan
  });
  it("at 1× a plain wheel up zooms when the page can't scroll up (a single big picture)", () => {
    expect(wheelAction(false, wheel(-100), false)).toBe("zoom");
  });
  it("ctrl + wheel (a trackpad pinch) zooms from 1×", () => {
    expect(wheelAction(false, wheel(-10, { ctrlKey: true }))).toBe("zoom");
    expect(wheelAction(false, wheel(10, { ctrlKey: true, deltaX: 30 }))).toBe("zoom");
  });
  it("once zoomed the wheel alone zooms, and a sideways swipe pans", () => {
    expect(wheelAction(true, wheel(-100))).toBe("zoom");
    expect(wheelAction(true, wheel(100))).toBe("zoom");
    expect(wheelAction(true, wheel(5, { deltaX: 40 }))).toBe("pan");
  });
});
