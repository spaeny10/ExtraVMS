import { describe, expect, it } from "vitest";
import {
  IDENTITY, MAX_SCALE, clampPan, clampScale, clientToLocal, contentBox, cssTurn, distance, isZoomed, midpoint, panBy, pinchXform, scaleLabel, toCss, wheelScale, zoomAt,
  type Point, type Rect, type Size, type Xform,
} from "./zoom";

const frame: Size = { width: 400, height: 300 };
const full: Rect = { left: 0, top: 0, width: 400, height: 300 };
/** where a frame point p shows after the transform */
const apply = (xf: Xform, p: Point): Point => ({ x: xf.x + xf.s * p.x, y: xf.y + xf.s * p.y });
/** the picture covers the frame on every axis where it is larger than the frame */
function covers(xf: Xform, f: Size, c: Rect) {
  const l = xf.x + xf.s * c.left, r = xf.x + xf.s * (c.left + c.width);
  const t = xf.y + xf.s * c.top, b = xf.y + xf.s * (c.top + c.height);
  if (xf.s * c.width >= f.width) { expect(l).toBeLessThanOrEqual(1e-9); expect(r).toBeGreaterThanOrEqual(f.width - 1e-9); }
  if (xf.s * c.height >= f.height) { expect(t).toBeLessThanOrEqual(1e-9); expect(b).toBeGreaterThanOrEqual(f.height - 1e-9); }
}

describe("contentBox (object-fit: contain)", () => {
  it("pillarboxes a picture narrower than the frame and letterboxes a wider one", () => {
    expect(contentBox({ width: 400, height: 300 }, 1)).toEqual({ left: 50, top: 0, width: 300, height: 300 });
    expect(contentBox({ width: 400, height: 300 }, 2)).toEqual({ left: 0, top: 50, width: 400, height: 200 });
  });
  it("uses the whole frame when the aspect is unknown", () => {
    expect(contentBox(frame, undefined)).toEqual(full);
    expect(contentBox(frame, 0)).toEqual(full);
    expect(contentBox(frame, NaN)).toEqual(full);
  });
});

describe("scale limits", () => {
  it("clamps to 1×–8×", () => {
    expect(clampScale(0.5)).toBe(1);
    expect(clampScale(20)).toBe(MAX_SCALE);
    expect(clampScale(NaN)).toBe(1);
    expect(clampScale(3)).toBe(3);
  });
  it("identity at 1× and an empty CSS transform", () => {
    expect(isZoomed(IDENTITY)).toBe(false);
    expect(toCss(IDENTITY)).toBe("");
    expect(toCss({ s: 2, x: -10, y: -20 })).toBe("translate(-10px, -20px) scale(2)");
  });
});

describe("zoomAt: zoom about a point", () => {
  it("keeps the point under the cursor fixed", () => {
    const p = { x: 120, y: 90 };
    const xf = zoomAt(IDENTITY, 2, p, frame, full);
    expect(xf.s).toBe(2);
    expect(apply(xf, p)).toEqual(p);
    // and again from an already zoomed state
    const q = { x: 300, y: 200 };
    const before = { x: (q.x - xf.x) / xf.s, y: (q.y - xf.y) / xf.s };
    const xf2 = zoomAt(xf, 3, q, frame, full);
    const after = apply(xf2, before);
    expect(after.x).toBeCloseTo(q.x);
    expect(after.y).toBeCloseTo(q.y);
  });
  it("zooming into the center keeps the picture centered", () => {
    const xf = zoomAt(IDENTITY, 4, { x: 200, y: 150 }, frame, full);
    expect(xf).toEqual({ s: 4, x: -600, y: -450 });
  });
  it("zooming near a corner is clamped so no empty space opens", () => {
    const xf = zoomAt(IDENTITY, 2, { x: 0, y: 0 }, frame, full);
    expect(xf).toEqual({ s: 2, x: 0, y: 0 });
    const xf2 = zoomAt(IDENTITY, 2, { x: 400, y: 300 }, frame, full);
    expect(xf2).toEqual({ s: 2, x: -400, y: -300 });
  });
  it("zooming back out to 1× returns exactly to identity", () => {
    const xf = zoomAt(IDENTITY, 3, { x: 50, y: 250 }, frame, full);
    expect(zoomAt(xf, 1, { x: 300, y: 10 }, frame, full)).toEqual(IDENTITY);
    expect(zoomAt(xf, 0.2, { x: 300, y: 10 }, frame, full)).toEqual(IDENTITY);
  });
  it("never exceeds 8×", () => {
    expect(zoomAt(IDENTITY, 50, { x: 1, y: 1 }, frame, full).s).toBe(8);
  });
});

describe("clampPan", () => {
  it("stops the picture's edges at the frame's edges", () => {
    const xf = { s: 2, x: 0, y: 0 };
    expect(panBy(xf, 100, 100, frame, full)).toEqual({ s: 2, x: 0, y: 0 });
    expect(panBy(xf, -1000, -1000, frame, full)).toEqual({ s: 2, x: -400, y: -300 });
    expect(panBy(xf, -50, -60, frame, full)).toEqual({ s: 2, x: -50, y: -60 });
  });
  it("keeps a letterboxed axis centered until the zoomed picture fills it", () => {
    const c = contentBox(frame, 2);           // 400×200 band in a 400×300 frame
    const xf = clampPan({ s: 1.2, x: -40, y: 37 }, frame, c);
    // 1.2 × 200 = 240 < 300: vertically centered: y + 1.2 × (50 + 100) = 150
    expect(xf.y + 1.2 * 150).toBeCloseTo(150);
    expect(xf.x).toBe(-40);
    const xf2 = clampPan({ s: 2, x: 0, y: 1000 }, frame, c);
    // 2 × 200 = 400 > 300: the band's top edge (y + 2 × 50) may not come below the frame's top
    expect(xf2.y + 2 * 50).toBeCloseTo(0);
    covers(xf2, frame, c);
  });
  it("pillarbox: the picture's side edges bound the pan once it is wider than the frame", () => {
    const c = contentBox(frame, 1);           // 300×300 square, 50 px bars left and right
    const xf = clampPan({ s: 3, x: 500, y: -2000 }, frame, c);
    expect(xf.x + 3 * c.left).toBeCloseTo(0);
    expect(xf.y + 3 * (c.top + c.height)).toBeCloseTo(300);
    covers(xf, frame, c);
  });
  it("scale 1 always resets the pan", () => {
    expect(clampPan({ s: 1, x: 40, y: -40 }, frame, full)).toEqual(IDENTITY);
  });
});

describe("wheelScale", () => {
  it("wheel up zooms in, wheel down zooms out, in smooth steps", () => {
    const up = wheelScale(1, -100);
    expect(up).toBeGreaterThan(1.15);
    expect(up).toBeLessThan(1.3);
    expect(wheelScale(2, 100)).toBeLessThan(2);
  });
  it("in and back out by the same amount returns to the same scale", () => {
    expect(wheelScale(wheelScale(2, -100), 100)).toBeCloseTo(2);
  });
  it("line and page delta modes are scaled to pixels", () => {
    expect(wheelScale(1, -3, 1)).toBeCloseTo(wheelScale(1, -120, 0));
    expect(wheelScale(1, -1, 2)).toBeGreaterThan(wheelScale(1, -100, 0));
  });
  it("stays within 1×–8× and caps a single fast flick", () => {
    expect(wheelScale(1, 500)).toBe(1);
    expect(wheelScale(8, -500)).toBe(8);
    expect(wheelScale(1, -100000)).toBeCloseTo(Math.exp(0.6));
  });
});

describe("pinch", () => {
  it("distance and midpoint", () => {
    expect(distance({ x: 0, y: 0 }, { x: 3, y: 4 })).toBe(5);
    expect(midpoint({ x: 10, y: 20 }, { x: 30, y: 60 })).toEqual({ x: 20, y: 40 });
  });
  it("spreading the fingers to twice the distance doubles the scale about the midpoint", () => {
    const mid = { x: 100, y: 100 };
    const xf = pinchXform({ xf: IDENTITY, dist: 50, mid }, 100, mid, frame, full);
    expect(xf.s).toBe(2);
    expect(apply(xf, mid)).toEqual(mid);
  });
  it("moving both fingers pans: the pinched point follows the midpoint", () => {
    const start = { xf: { s: 2, x: -100, y: -100 }, dist: 80, mid: { x: 200, y: 150 } };
    const xf = pinchXform(start, 80, { x: 180, y: 140 }, frame, full);
    expect(xf).toEqual({ s: 2, x: -120, y: -110 });
  });
  it("pinching in past 1× snaps back to identity", () => {
    const xf = pinchXform({ xf: { s: 2, x: -100, y: -50 }, dist: 200, mid: { x: 150, y: 150 } }, 20, { x: 150, y: 150 }, frame, full);
    expect(xf).toEqual(IDENTITY);
  });
  it("is clamped to the frame", () => {
    const xf = pinchXform({ xf: IDENTITY, dist: 50, mid: { x: 5, y: 5 } }, 200, { x: 300, y: 250 }, frame, full);
    covers(xf, frame, full);
    expect(xf.s).toBe(4);
  });
});

describe("scaleLabel", () => {
  it("one decimal, none when whole", () => {
    expect(scaleLabel(2)).toBe("2×");
    expect(scaleLabel(2.54)).toBe("2.5×");
    expect(scaleLabel(7.96)).toBe("8×");
  });
});

describe("clientToLocal: pointer positions under full screen's CSS turn", () => {
  // a phone held upright, 375 × 812; the stage (and the zoom frame filling it) laid out 812 × 375 and turned with
  // fullscreen.css: rotate(90deg) translateY(-100%), origin top left
  const W = 375, H = 812;
  const size: Size = { width: H, height: W };
  const rect: Rect = { left: 0, top: 0, width: W, height: H };   // getBoundingClientRect of the turned frame
  /** where the CSS puts a frame point on screen: translateY(-W), then rotate 90° clockwise ((x, y) → (−y, x)) */
  const screenOf = (p: Point): Point => ({ x: -(p.y - W), y: p.x });
  const close = (a: Point, b: Point) => { expect(a.x).toBeCloseTo(b.x, 6); expect(a.y).toBeCloseTo(b.y, 6); };

  it("unturned: the offset from the frame's corner, as before", () => {
    expect(clientToLocal({ x: 130, y: 90 }, { left: 30, top: 40, width: 400, height: 300 }, frame)).toEqual({ x: 100, y: 50 });
    expect(clientToLocal({ x: 130, y: 90 }, { left: 30, top: 40, width: 400, height: 300 }, frame, 360)).toEqual({ x: 100, y: 50 });
  });
  it("the CSS puts the frame exactly over the screen, its top edge down the right side", () => {
    close(screenOf({ x: 0, y: 0 }), { x: W, y: 0 });
    close(screenOf({ x: H, y: 0 }), { x: W, y: H });
    close(screenOf({ x: 0, y: W }), { x: 0, y: 0 });
    close(screenOf({ x: H, y: W }), { x: 0, y: H });
  });
  it("turned 90°: the corners and the centre map back", () => {
    expect(clientToLocal({ x: W, y: 0 }, rect, size, 90)).toEqual({ x: 0, y: 0 });
    expect(clientToLocal({ x: W, y: H }, rect, size, 90)).toEqual({ x: H, y: 0 });
    expect(clientToLocal({ x: 0, y: 0 }, rect, size, 90)).toEqual({ x: 0, y: W });
    expect(clientToLocal({ x: 0, y: H }, rect, size, 90)).toEqual({ x: H, y: W });
    expect(clientToLocal({ x: W / 2, y: H / 2 }, rect, size, 90)).toEqual({ x: H / 2, y: W / 2 });
  });
  it("is the inverse of the CSS turn for any point", () => {
    for (const p of [{ x: 10, y: 20 }, { x: 600, y: 300 }, { x: 406, y: 1 }, { x: 811, y: 374 }]) close(clientToLocal(screenOf(p), rect, size, 90), p);
  });
  it("any angle: the inverse of a turn about the frame's centre (offset rect, as in a dialog)", () => {
    const f: Size = { width: 300, height: 200 };
    for (const deg of [30, 90, 180, 270, -90]) {
      const r = (deg * Math.PI) / 180, c = Math.cos(r), sn = Math.sin(r);
      // the box around the turned frame, centred at (500, 400)
      const bw = Math.abs(f.width * c) + Math.abs(f.height * sn), bh = Math.abs(f.width * sn) + Math.abs(f.height * c);
      const box: Rect = { left: 500 - bw / 2, top: 400 - bh / 2, width: bw, height: bh };
      const toScreen = (p: Point): Point => {
        const dx = p.x - f.width / 2, dy = p.y - f.height / 2;
        return { x: 500 + dx * c - dy * sn, y: 400 + dx * sn + dy * c };
      };
      for (const p of [{ x: 0, y: 0 }, { x: 300, y: 0 }, { x: 120, y: 170 }]) close(clientToLocal(toScreen(p), box, f, deg), p);
    }
  });
  it("a pan moves the picture with the finger on the glass", () => {
    const content = contentBox(size, 2592 / 1520);
    const xf = zoomAt(IDENTITY, 3, { x: H / 2, y: W / 2 }, size, content);
    // the finger slides 40 px down the screen (to the viewer holding the phone sideways: to the right)
    const a = { x: 200, y: 300 }, b = { x: 200, y: 340 };
    const la = clientToLocal(a, rect, size, 90), lb = clientToLocal(b, rect, size, 90);
    expect(lb.x - la.x).toBeCloseTo(40, 6);
    expect(lb.y - la.y).toBeCloseTo(0, 6);
    const moved = panBy(xf, lb.x - la.x, lb.y - la.y, size, content);
    // the picture point that was under the finger is under it again
    const q = { x: (la.x - xf.x) / xf.s, y: (la.y - xf.y) / xf.s };
    close(screenOf(apply(moved, q)), b);
  });
  it("a pinch keeps the picture point between the fingers there", () => {
    const content = contentBox(size, 2592 / 1520);
    const a0 = { x: 150, y: 350 }, b0 = { x: 250, y: 450 };
    const l = (p: Point) => clientToLocal(p, rect, size, 90);
    const start = { xf: IDENTITY, dist: distance(l(a0), l(b0)), mid: midpoint(l(a0), l(b0)) };
    const a1 = { x: 120, y: 320 }, b1 = { x: 280, y: 480 };   // spread apart about the same middle
    const xf = pinchXform(start, distance(l(a1), l(b1)), midpoint(l(a1), l(b1)), size, content);
    expect(xf.s).toBeCloseTo(1.6, 6);
    close(screenOf(apply(xf, start.mid)), { x: 200, y: 400 });
  });
});

describe("cssTurn: the turn in a computed transform", () => {
  it("reads the matrix", () => {
    expect(cssTurn("none")).toBe(0);
    expect(cssTurn(undefined)).toBe(0);
    expect(cssTurn("matrix(1, 0, 0, 1, 0, 0)")).toBe(0);
    expect(cssTurn("matrix(0, 1, -1, 0, 375, 0)")).toBe(90);
    expect(cssTurn("matrix(6.12323e-17, 1, -1, 6.12323e-17, 375, 0)")).toBe(90);
    expect(cssTurn("matrix(-1, 0, 0, -1, 0, 0)")).toBe(180);
    expect(cssTurn("matrix(0, -1, 1, 0, 0, 0)")).toBe(270);
    expect(cssTurn("matrix3d(0, 1, 0, 0, -1, 0, 0, 0, 0, 0, 1, 0, 375, 0, 0, 1)")).toBe(90);
    expect(cssTurn("matrix(2, 0, 0, 2, 0, 0)")).toBe(0);   // a scale is no turn
  });
});
