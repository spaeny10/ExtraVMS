/**
 * Time-axis zoom, pan and hover math shared by the Timeline page (Timeline.tsx) and the event viewer's strip
 * (EventPlayer.tsx): mouse-wheel zoom around the cursor, shift / horizontal wheel pans, pinch, drag to pan versus a
 * click, which time a hover thumbnail asks for, and where that thumbnail sits. Pure: windows are epoch seconds,
 * positions CSS pixels. Each caller clamps the result to its own limits (the Timeline's clampView, the strip's
 * clampStrip in eventPlayback.ts).
 */
import { LIVE_LAG } from "./playback";

export type TimeWindow = { start: number; end: number };
/** Shortest and longest window, seconds. */
export type RangeLimits = { min: number; max: number };

/** How fast the wheel zooms: the window grows by e^(0.0015·px), so one 100 px mouse notch is about ×1.16. */
export const WHEEL_RATE = 0.0015;
/** A press that moves further than this (CSS px) is a drag (pan, scrub); less is a click. A fingertip wobbles more. */
export const DRAG_PX = 3;
export const TOUCH_DRAG_PX = 8;
/** Pinch: finger spreads under this count as this (two fingers that close never divide by ~0). */
const PINCH_MIN_PX = 20;

export const clampRange = (r: number, lim: RangeLimits): number => Math.min(lim.max, Math.max(lim.min, r));
/** 0..1 across a box at clientX (clamped). */
export const fracIn = (clientX: number, box: { left: number; width: number }): number =>
  Math.min(1, Math.max(0, (clientX - box.left) / Math.max(1, box.width)));
/** The time at x px across a window drawn `widthPx` wide (not clamped: the pointer can be outside while dragging). */
export const timeAtPx = (x: number, w: TimeWindow, widthPx: number): number => w.start + (x / Math.max(1, widthPx)) * (w.end - w.start);

/** A wheel delta in pixels: deltaMode 1 = lines (40 px), 2 = pages (800 px). */
export const wheelPx = (delta: number, deltaMode = 0): number => (deltaMode === 1 ? delta * 40 : deltaMode === 2 ? delta * 800 : delta);

/** The window `range` seconds wide that keeps the time at `frac` across `w` where it is on screen. */
export function zoomAround(w: TimeWindow, frac: number, range: number): TimeWindow {
  const anchor = w.start + frac * (w.end - w.start);
  return { start: anchor - frac * range, end: anchor + (1 - frac) * range };
}

/** Wheel zoom around the cursor (`frac` across): down / towards you zooms out, up zooms in. */
export function wheelZoom(w: TimeWindow, frac: number, deltaY: number, deltaMode: number, lim: RangeLimits): TimeWindow {
  return zoomAround(w, frac, clampRange((w.end - w.start) * Math.exp(wheelPx(deltaY, deltaMode) * WHEEL_RATE), lim));
}

/** Move the window by `px` of a strip `widthPx` wide (positive: later). */
export const shiftByPx = (w: TimeWindow, px: number, widthPx: number): TimeWindow => {
  const d = (px / Math.max(1, widthPx)) * (w.end - w.start);
  return { start: w.start + d, end: w.end + d };
};

/** One wheel event: shift+wheel or a mostly horizontal swipe pans, anything else zooms around the cursor. */
export function wheelWindow(w: TimeWindow, ev: { deltaX: number; deltaY: number; deltaMode: number; shiftKey: boolean },
  frac: number, widthPx: number, lim: RangeLimits): TimeWindow {
  if (ev.shiftKey || Math.abs(ev.deltaX) > Math.abs(ev.deltaY)) return shiftByPx(w, ev.shiftKey ? ev.deltaY : ev.deltaX, widthPx);
  return wheelZoom(w, frac, ev.deltaY, ev.deltaMode, lim);
}

/** Drag to pan: the window the drag started with (`w0`), moved so the time under the pointer follows it `dx` px. */
export const panWindow = (w0: TimeWindow, dx: number, widthPx: number): TimeWindow => shiftByPx(w0, -dx, widthPx);

/** Is a press that has moved `dx` px a drag yet? */
export const isDrag = (dx: number, pointerType = "mouse"): boolean => Math.abs(dx) > (pointerType === "touch" ? TOUCH_DRAG_PX : DRAG_PX);

/** Where a two-finger pinch started: the finger spread (px), their midpoint (clientX) and the window then. */
export type PinchStart = { d0: number; mid0: number; view0: TimeWindow };
export const pinchStart = (x0: number, x1: number, view0: TimeWindow): PinchStart =>
  ({ d0: Math.max(PINCH_MIN_PX, Math.abs(x0 - x1)), mid0: (x0 + x1) / 2, view0 });

/** A pinch in progress (fingers at clientX x0, x1): the range follows the spread, the time under the starting midpoint
 *  stays under the fingers, and moving both fingers pans. */
export function pinchWindow(p: PinchStart, x0: number, x1: number, box: { left: number; width: number }, lim: RangeLimits): TimeWindow {
  const d = Math.max(PINCH_MIN_PX, Math.abs(x0 - x1));
  const r0 = p.view0.end - p.view0.start;
  const nr = clampRange(r0 * (p.d0 / d), lim);
  const z = zoomAround(p.view0, fracIn(p.mid0, box), nr);
  const shift = -(((x0 + x1) / 2 - p.mid0) / Math.max(1, box.width)) * nr;
  return { start: z.start + shift, end: z.end + shift };
}

/** The time a hover thumbnail asks for at `t`: snapped to whole seconds, so nearby positions share one URL (and the
 *  browser's cache of it: past frames are cacheable for a day); null this close to now, where nothing is on disk yet. */
export const hoverFrameTime = (t: number, now: number): number | null => (t < now - LIVE_LAG ? Math.round(t) : null);

/** Left edge (px) of a thumbnail `thumbW` wide centred on x, kept inside a box `boxW` wide. */
export const thumbLeft = (x: number, thumbW: number, boxW: number): number => Math.max(0, Math.min(x - thumbW / 2, boxW - thumbW));
