/**
 * Digital zoom math for a video shown with object-fit: contain inside a frame (VideoZoom.tsx).
 *
 * The zoomed layer is drawn with `transform: translate(x, y) scale(s)` and transform-origin 0 0, so a point p of
 * the unzoomed frame lands at (x + s·p.x, y + s·p.y). Everything here is pure: frame sizes and points are in CSS
 * pixels relative to the frame's top-left corner.
 */

export type Point = { x: number; y: number };
export type Size = { width: number; height: number };
export type Rect = { left: number; top: number; width: number; height: number };
/** scale `s` and translation (x, y) of the zoomed layer */
export type Xform = { s: number; x: number; y: number };

export const MIN_SCALE = 1;
export const MAX_SCALE = 8;
export const IDENTITY: Xform = { s: 1, x: 0, y: 0 };
/** keyboard / button step: ×1.25 per press */
export const STEP = 1.25;

export const clampScale = (s: number) => (Number.isFinite(s) ? Math.min(MAX_SCALE, Math.max(MIN_SCALE, s)) : MIN_SCALE);
export const isZoomed = (xf: Xform) => xf.s > 1.001;

/** Where the picture sits inside the frame (object-fit: contain). No aspect (metadata not loaded yet): the whole frame. */
export function contentBox(frame: Size, aspect?: number): Rect {
  if (!aspect || !Number.isFinite(aspect) || aspect <= 0 || frame.width <= 0 || frame.height <= 0) {
    return { left: 0, top: 0, width: frame.width, height: frame.height };
  }
  if (frame.width / frame.height > aspect) {
    const w = frame.height * aspect;
    return { left: (frame.width - w) / 2, top: 0, width: w, height: frame.height };
  }
  const h = frame.width / aspect;
  return { left: 0, top: (frame.height - h) / 2, width: frame.width, height: h };
}

/** One axis of clampPan: picture smaller than the frame → centered; larger → its edges may not come inside the frame. */
function clampAxis(t: number, s: number, frameLen: number, start: number, len: number): number {
  const scaled = s * len;
  const v = scaled <= frameLen ? frameLen / 2 - s * (start + len / 2) : Math.min(-s * start, Math.max(frameLen - s * (start + len), t));
  return v + 0;   // no -0
}

/**
 * Keep the picture covering the frame: no empty space opens beside a picture larger than the frame, and an axis
 * where the zoomed picture is still smaller than the frame (letterbox) stays centered. Scale 1 → identity.
 */
export function clampPan(xf: Xform, frame: Size, content: Rect): Xform {
  const s = clampScale(xf.s);
  if (s <= 1.001) return IDENTITY;
  return {
    s,
    x: clampAxis(xf.x, s, frame.width, content.left, content.width),
    y: clampAxis(xf.y, s, frame.height, content.top, content.height),
  };
}

/** Zoom to scale `s` keeping the picture point under `p` (frame coordinates) where it is, then clamp. */
export function zoomAt(xf: Xform, s: number, p: Point, frame: Size, content: Rect): Xform {
  const next = clampScale(s);
  const qx = (p.x - xf.x) / xf.s, qy = (p.y - xf.y) / xf.s;   // the unzoomed point under p
  return clampPan({ s: next, x: p.x - next * qx, y: p.y - next * qy }, frame, content);
}

/** Move the zoomed picture by (dx, dy), clamped. */
export function panBy(xf: Xform, dx: number, dy: number, frame: Size, content: Rect): Xform {
  return clampPan({ s: xf.s, x: xf.x + dx, y: xf.y + dy }, frame, content);
}

/**
 * The scale after one wheel event: smooth exponential steps (a mouse notch of 100 px ≈ ×1.22), so zooming in
 * and back out by the same amount returns to the same scale. deltaMode 1 = lines, 2 = pages.
 */
export function wheelScale(s: number, deltaY: number, deltaMode = 0): number {
  const px = deltaMode === 1 ? deltaY * 40 : deltaMode === 2 ? deltaY * 800 : deltaY;
  const step = Math.max(-300, Math.min(300, px));          // one fast flick never jumps more than ×1.8
  return clampScale(s * Math.exp(-step * 0.002));
}

export const distance = (a: Point, b: Point) => Math.hypot(a.x - b.x, a.y - b.y);
export const midpoint = (a: Point, b: Point): Point => ({ x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 });

/** Where a two-finger pinch started: the transform then, the finger spread and the midpoint. */
export type PinchStart = { xf: Xform; dist: number; mid: Point };

/**
 * The transform for a pinch in progress: the scale follows the finger spread, and the picture point that was under
 * the starting midpoint follows the current midpoint (so moving both fingers also pans).
 */
export function pinchXform(start: PinchStart, dist: number, mid: Point, frame: Size, content: Rect): Xform {
  const s = clampScale(start.xf.s * (dist / Math.max(1, start.dist)));
  const qx = (start.mid.x - start.xf.x) / start.xf.s, qy = (start.mid.y - start.xf.y) / start.xf.s;
  return clampPan({ s, x: mid.x - s * qx, y: mid.y - s * qy }, frame, content);
}

/** CSS transform for a layer (transform-origin 0 0). */
export const toCss = (xf: Xform) => (isZoomed(xf) ? `translate(${xf.x}px, ${xf.y}px) scale(${xf.s})` : "");

/** "2.5×" for the indicator: one decimal, none when whole ("2×"). */
export const scaleLabel = (s: number) => `${Math.round(s * 10) / 10}×`;
