import { describe, expect, it } from "vitest";
import { LIVE_LAG, frameKey } from "./playback";
import {
  DRAG_PX, TOUCH_DRAG_PX, WHEEL_RATE, clampRange, fracIn, hoverFrameTime, isDrag, panWindow, pinchStart, pinchWindow, shiftByPx,
  thumbLeft, timeAtPx, wheelPx, wheelWindow, wheelZoom, zoomAround,
} from "./timeZoom";

const W = { start: 1000, end: 1600 };   // 600 s
const LIM = { min: 60, max: 14 * 86400 };
const wheel = (deltaY: number, o: Partial<{ deltaX: number; deltaMode: number; shiftKey: boolean }> = {}) =>
  ({ deltaX: 0, deltaY, deltaMode: 0, shiftKey: false, ...o });

describe("zoom at the cursor", () => {
  it("keeps the time under the cursor where it is", () => {
    const z = zoomAround(W, 0.25, 300);
    expect(z.end - z.start).toBe(300);
    expect(z.start + 0.25 * 300).toBe(W.start + 0.25 * 600);   // 1150 stays a quarter of the way across
  });
  it("wheel down zooms out, up zooms in, by the same factor both ways", () => {
    const out = wheelZoom(W, 0.5, 100, 0, LIM);
    expect(out.end - out.start).toBeCloseTo(600 * Math.exp(100 * WHEEL_RATE));
    expect((out.start + out.end) / 2).toBeCloseTo(1300);
    const back = wheelZoom(out, 0.5, -100, 0, LIM);
    expect(back.start).toBeCloseTo(W.start);
    expect(back.end).toBeCloseTo(W.end);
  });
  it("line and page wheels count as pixels", () => {
    expect(wheelPx(3, 1)).toBe(120);
    expect(wheelPx(1, 2)).toBe(800);
    expect(wheelPx(53, 0)).toBe(53);
  });
  it("stops at the limits", () => {
    expect(clampRange(10, LIM)).toBe(60);
    const z = wheelZoom(W, 0, -100000, 0, { min: 60, max: 7200 });
    expect(z).toEqual({ start: 1000, end: 1060 });   // the left edge was under the cursor
    const o = wheelZoom(W, 1, 100000, 0, { min: 60, max: 7200 });
    expect(o.end).toBe(1600);
    expect(o.end - o.start).toBe(7200);
  });
  it("shift+wheel and sideways swipes pan instead", () => {
    expect(wheelWindow(W, wheel(100, { shiftKey: true }), 0.5, 600, LIM)).toEqual({ start: 1100, end: 1700 });
    expect(wheelWindow(W, wheel(5, { deltaX: -60 }), 0.5, 600, LIM)).toEqual({ start: 940, end: 1540 });
    expect(shiftByPx(W, 300, 600)).toEqual({ start: 1300, end: 1900 });
  });
  it("a pinch follows the finger spread around its midpoint, and both fingers moving pans", () => {
    const box = { left: 0, width: 600 };
    const p = pinchStart(200, 400, W);   // midpoint 300 px = 1300 s
    const z = pinchWindow(p, 100, 500, box, LIM);   // fingers twice as far apart: half the range
    expect(z.end - z.start).toBeCloseTo(300);
    expect(z.start + 0.5 * 300).toBeCloseTo(1300);
    const moved = pinchWindow(p, 260, 460, box, LIM);   // same spread, 60 px to the right: 60 s earlier
    expect(moved.start).toBeCloseTo(940);
    expect(pinchStart(300, 305, W).d0).toBe(20);   // fingers together never divide by ~0
  });
});

describe("drag or click", () => {
  it("a few pixels is still a click; a fingertip gets more slack", () => {
    expect(isDrag(DRAG_PX)).toBe(false);
    expect(isDrag(DRAG_PX + 1)).toBe(true);
    expect(isDrag(-(DRAG_PX + 1))).toBe(true);
    expect(isDrag(TOUCH_DRAG_PX, "touch")).toBe(false);
    expect(isDrag(TOUCH_DRAG_PX + 1, "touch")).toBe(true);
  });
  it("dragging moves the time under the pointer with it", () => {
    expect(panWindow(W, 60, 600)).toEqual({ start: 940, end: 1540 });   // dragged right: earlier footage comes in
    expect(panWindow(W, -300, 600)).toEqual({ start: 1300, end: 1900 });
  });
});

describe("hover", () => {
  it("maps pixels to times", () => {
    expect(fracIn(150, { left: 100, width: 200 })).toBe(0.25);
    expect(fracIn(50, { left: 100, width: 200 })).toBe(0);
    expect(fracIn(900, { left: 100, width: 200 })).toBe(1);
    expect(timeAtPx(150, W, 600)).toBe(1150);
    expect(timeAtPx(-60, W, 600)).toBe(940);   // a drag can leave the strip
  });
  it("asks for whole seconds, so nearby positions share a frame URL (and its cache)", () => {
    const now = 10_000;
    expect(hoverFrameTime(1234.4, now)).toBe(1234);
    expect(hoverFrameTime(1234.6, now)).toBe(1235);
    expect(hoverFrameTime(now - LIVE_LAG + 0.5, now)).toBeNull();   // not on disk yet
    expect(frameKey("cam1", hoverFrameTime(1234.4, now)!)).toBe(frameKey("cam1", hoverFrameTime(1233.6, now)!));
    expect(frameKey("cam1", 1234)).not.toBe(frameKey("cam1", 1234, true));
    expect(frameKey("cam1", 1234)).not.toBe(frameKey("cam2", 1234));
  });
  it("keeps the thumbnail centred on the cursor but inside the box", () => {
    expect(thumbLeft(400, 320, 1000)).toBe(240);
    expect(thumbLeft(20, 320, 1000)).toBe(0);
    expect(thumbLeft(990, 320, 1000)).toBe(680);
    expect(thumbLeft(100, 320, 300)).toBe(0);   // narrower than the thumbnail
  });
});
