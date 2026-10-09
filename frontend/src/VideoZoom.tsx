/**
 * Digital zoom for a video picture (Live and Timeline): mouse wheel zooms toward the cursor, left-drag pans while
 * zoomed, double-click resets; on touch, pinch zooms about the fingers, one finger pans while zoomed, double-tap
 * resets. A CSS transform on a layer around the <video> (zoom.ts has the math): no re-encoding, nothing server-side.
 *
 *   <ZoomFrame>       wraps the <video> inside a player box; owns the gestures and the "2.5× · Reset" pill.
 *   <ZoomScope>       optional, around a whole tile: the zoom state outlives the player inside it (a Timeline tile's
 *                     chunk videos come and go, Live falls back from WebRTC to recordings), resets when `resetKey`
 *                     changes (another camera), and is switched off with `disabled` (PTZ mode owns the picture).
 *   <ZoomLayer>       an overlay drawn over the picture (painted region, scrub preview) that must move with it:
 *                     gets the same transform. Without a scope it renders its children unchanged.
 *
 * A ZoomFrame without a scope keeps its own state, so any player can use it alone.
 */
import { createContext, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  IDENTITY, STEP, clampPan, contentBox, distance, isZoomed, midpoint, pinchXform, scaleLabel, toCss, wheelScale, zoomAt,
  type PinchStart, type Point, type Rect, type Size, type Xform,
} from "./zoom";
import "./videoZoom.css";

type Listener = (xf: Xform, animate: boolean) => void;

/** The zoom state of one picture, shared by its frame and any overlay layers. */
export class ZoomController {
  xf: Xform = IDENTITY;
  private frame: HTMLElement | null = null;
  private size: Size | null = null;
  private listeners = new Set<Listener>();

  attach(el: HTMLElement) {
    this.frame = el;
    this.size = null;
    this.resized();
    return () => { if (this.frame === el) this.frame = null; };
  }
  subscribe(fn: Listener) {
    this.listeners.add(fn);
    fn(this.xf, false);
    return () => { this.listeners.delete(fn); };
  }
  /** the frame's size and where the picture sits in it (object-fit: contain, from the video's real aspect) */
  geometry(): { frame: Size; content: Rect } | null {
    const el = this.frame;
    if (!el || !el.clientWidth || !el.clientHeight) return null;
    const frame = { width: el.clientWidth, height: el.clientHeight };
    const v = el.querySelector("video");
    return { frame, content: contentBox(frame, v && v.videoWidth && v.videoHeight ? v.videoWidth / v.videoHeight : undefined) };
  }
  set(xf: Xform, animate = false) {
    const cur = this.xf;
    if (cur.s === xf.s && cur.x === xf.x && cur.y === xf.y) return;
    this.xf = xf;
    this.listeners.forEach((fn) => fn(xf, animate));
  }
  reset(animate = true) { this.set(IDENTITY, animate); }
  /** zoom to scale `s` about `p` (frame coordinates; default: the frame's center) */
  zoomTo(s: number, p?: Point, animate = false) {
    const g = this.geometry();
    if (!g) return;
    this.set(zoomAt(this.xf, s, p ?? { x: g.frame.width / 2, y: g.frame.height / 2 }, g.frame, g.content), animate);
  }
  /** set a transform computed elsewhere, clamped to the current geometry */
  clamped(xf: Xform, animate = false) {
    const g = this.geometry();
    if (g) this.set(clampPan(xf, g.frame, g.content), animate);
  }
  panBy(dx: number, dy: number, animate = false) { this.clamped({ ...this.xf, x: this.xf.x + dx, y: this.xf.y + dy }, animate); }
  /** the frame changed size (layout) or the video its aspect: keep the same part of the picture in view */
  resized() {
    const g = this.geometry();
    if (!g) return;
    const prev = this.size;
    this.size = g.frame;
    const xf = this.xf;
    if (prev && (prev.width !== g.frame.width || prev.height !== g.frame.height)) {
      this.clamped({ s: xf.s, x: xf.x * g.frame.width / prev.width, y: xf.y * g.frame.height / prev.height });
    } else this.clamped(xf);
  }
}

type Scope = { ctl: ZoomController; disabled: boolean; doubleClickReset: boolean; keyboard: boolean };
const ZoomContext = createContext<Scope | null>(null);

/**
 * Shares one zoom state across everything a tile shows. `resetKey`: back to 1× when it changes (another camera).
 * `disabled`: no zoom gestures and back to 1× (e.g. PTZ mode, whose drag/click/wheel steer the camera).
 * `doubleClickReset` false: the tile already uses double-click (the Timeline isolates a camera), so only the pill's
 * Reset button goes back to 1×. `keyboard` false: the page already uses + / − / 0 (the Timeline's own zoom).
 */
export function ZoomScope({ resetKey, disabled = false, doubleClickReset = true, keyboard = true, children }: {
  resetKey?: unknown; disabled?: boolean; doubleClickReset?: boolean; keyboard?: boolean; children: ReactNode;
}) {
  const [ctl] = useState(() => new ZoomController());
  useEffect(() => { ctl.reset(false); }, [ctl, resetKey]);
  useEffect(() => { if (disabled) ctl.reset(false); }, [ctl, disabled]);
  const value = useMemo(() => ({ ctl, disabled, doubleClickReset, keyboard }), [ctl, disabled, doubleClickReset, keyboard]);
  return <ZoomContext.Provider value={value}>{children}</ZoomContext.Provider>;
}

function applyXform(el: HTMLElement, xf: Xform, animate: boolean) {
  el.style.transition = animate ? "transform .12s ease-out" : "none";
  el.style.transform = toCss(xf);
  el.style.willChange = isZoomed(xf) ? "transform" : "";
}

/** An overlay that must stay on the zoomed picture: same box as the frame, same transform. */
export function ZoomLayer({ children }: { children: ReactNode }) {
  const scope = useContext(ZoomContext);
  const inner = useRef<HTMLDivElement>(null);
  const ctl = scope?.ctl;
  useLayoutEffect(() => {
    const el = inner.current;
    if (!ctl || !el) return;
    return ctl.subscribe((xf, animate) => applyXform(el, xf, animate));
  }, [ctl]);
  if (!scope) return <>{children}</>;
  return <div className="vzoom-layer"><div ref={inner} className="vzoom-layer-inner">{children}</div></div>;
}

const TAP_MS = 300;     // a double-tap's two taps within this long...
const TAP_PX = 30;      // ...and this close together
const MOVE_PX = 6;      // a press that moves more than this is a drag, not a tap or click

type Gesture = {
  pointers: Map<number, Point>;
  pan: { from: Point; xf: Xform } | null;
  pinch: PinchStart | null;
  moved: boolean;       // a pointer moved past MOVE_PX: not a tap
  acted: boolean;       // this press panned or pinched the picture: its click is swallowed
  down: { at: number; p: Point; touch: boolean } | null;
  lastTap: { at: number; p: Point } | null;
  touchOurs: boolean;   // this touch sequence pinches or pans: the tile's swipe-to-switch must not see it
};

/**
 * The zoomable box around a <video>. Fills its positioned parent (the player box); the video inside is laid out as
 * before. Props apply when there is no ZoomScope around it.
 */
export function ZoomFrame({ children, doubleClickReset: dblProp = true, keyboard: keysProp = true }: {
  children: ReactNode; doubleClickReset?: boolean; keyboard?: boolean;
}) {
  const scope = useContext(ZoomContext);
  const [own] = useState(() => new ZoomController());
  const ctl = scope?.ctl ?? own;
  const disabled = scope?.disabled ?? false;
  const dblReset = scope?.doubleClickReset ?? dblProp;
  const keyboard = scope?.keyboard ?? keysProp;
  const opts = useRef({ disabled, dblReset });
  opts.current = { disabled, dblReset };
  const frame = useRef<HTMLDivElement>(null);
  const inner = useRef<HTMLDivElement>(null);
  const [view, setView] = useState({ zoomed: false, label: "1×" });
  const g = useRef<Gesture>({ pointers: new Map(), pan: null, pinch: null, moved: false, acted: false, down: null, lastTap: null, touchOurs: false });

  useLayoutEffect(() => {
    const el = frame.current, layer = inner.current;
    if (!el || !layer) return;
    const detach = ctl.attach(el);
    const unsub = ctl.subscribe((xf, animate) => {
      applyXform(layer, xf, animate);
      const zoomed = isZoomed(xf);
      el.classList.toggle("zoomed", zoomed);
      const label = scaleLabel(xf.s);
      setView((v) => (v.zoomed === zoomed && v.label === label ? v : { zoomed, label }));
    });
    const ro = new ResizeObserver(() => ctl.resized());
    ro.observe(el);
    // media events don't bubble, but they do pass ancestors in the capture phase: the video's real aspect arrived
    const meta = () => ctl.resized();
    el.addEventListener("loadedmetadata", meta, true);
    el.addEventListener("resize", meta, true);
    // wheel: non-passive so zooming doesn't scroll the page; at 1× a wheel toward "zoom out" is left to the page
    const wheel = (ev: WheelEvent) => {
      if (opts.current.disabled) return;
      const zoomed = isZoomed(ctl.xf);
      if (!ev.ctrlKey && Math.abs(ev.deltaX) > Math.abs(ev.deltaY)) {   // sideways swipe on a trackpad: pan
        if (!zoomed) return;
        ev.preventDefault();
        ctl.panBy(-ev.deltaX * (ev.deltaMode === 1 ? 40 : 1), 0);
        return;
      }
      if (!zoomed && ev.deltaY >= 0 && !ev.ctrlKey) return;
      ev.preventDefault();   // ctrl+wheel (a trackpad pinch) must not zoom the whole page either
      const r = el.getBoundingClientRect();
      const notch = ev.deltaMode !== 0 || Math.abs(ev.deltaY) >= 50;   // a mouse wheel click, not a trackpad stream
      ctl.zoomTo(wheelScale(ctl.xf.s, ev.deltaY * (ev.ctrlKey ? 4 : 1), ev.deltaMode), { x: ev.clientX - r.left, y: ev.clientY - r.top }, notch);
    };
    el.addEventListener("wheel", wheel, { passive: false });
    // touch: our pinch and pan must not scroll or zoom the page (Safari also needs its gesture events stopped)
    const touchMove = (ev: TouchEvent) => {
      if (!opts.current.disabled && ev.cancelable && (ev.touches.length > 1 || isZoomed(ctl.xf))) ev.preventDefault();
    };
    const gesture = (ev: Event) => { if (!opts.current.disabled) ev.preventDefault(); };
    el.addEventListener("touchmove", touchMove, { passive: false });
    el.addEventListener("gesturestart", gesture);
    el.addEventListener("gesturechange", gesture);
    return () => {
      unsub(); detach(); ro.disconnect();
      el.removeEventListener("loadedmetadata", meta, true);
      el.removeEventListener("resize", meta, true);
      el.removeEventListener("wheel", wheel);
      el.removeEventListener("touchmove", touchMove);
      el.removeEventListener("gesturestart", gesture);
      el.removeEventListener("gesturechange", gesture);
    };
  }, [ctl]);

  const local = (e: { clientX: number; clientY: number }): Point => {
    const r = frame.current!.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  };
  const capture = (id: number) => { try { frame.current?.setPointerCapture(id); } catch { /* pointer already gone */ } };
  const startPinch = () => {
    const [a, b] = [...g.current.pointers.values()];
    g.current.pinch = { xf: ctl.xf, dist: distance(a, b), mid: midpoint(a, b) };
    g.current.pan = null;
    g.current.moved = true;
    g.current.acted = true;
  };
  const endGesture = () => {
    const s = g.current;
    s.pan = null; s.pinch = null;
    frame.current?.classList.remove("panning");
  };

  const onPointerDown = (e: React.PointerEvent) => {
    if (opts.current.disabled || (e.target as Element).closest(".vzoom-pill")) return;
    if (e.pointerType === "mouse" && e.button !== 0) return;
    const s = g.current;
    if (keyboard) frame.current?.focus({ preventScroll: true });
    const p = local(e);
    if (s.pointers.size === 0) { s.moved = false; s.acted = false; s.down = { at: Date.now(), p, touch: e.pointerType !== "mouse" }; }
    s.pointers.set(e.pointerId, p);
    if (s.pointers.size === 2) {
      // two fingers: pinch (both captured so they keep reporting if they slide off the picture)
      s.pointers.forEach((_, id) => capture(id));
      startPinch();
      e.stopPropagation();
    } else if (s.pointers.size === 1 && isZoomed(ctl.xf)) {
      // zoomed: this press pans the picture, so the tile's own drag (Timeline reorder) and selection don't start
      capture(e.pointerId);
      s.pan = { from: p, xf: ctl.xf };
      frame.current?.classList.add("panning");
      e.stopPropagation();
      if (e.pointerType === "mouse") e.preventDefault();
    }
  };
  const onPointerMove = (e: React.PointerEvent) => {
    const s = g.current;
    if (!s.pointers.has(e.pointerId)) return;
    const p = local(e);
    s.pointers.set(e.pointerId, p);
    if (s.down && Math.hypot(p.x - s.down.p.x, p.y - s.down.p.y) > MOVE_PX) s.moved = true;
    const geo = ctl.geometry();
    if (!geo) return;
    if (s.pinch && s.pointers.size >= 2) {
      const [a, b] = [...s.pointers.values()];
      ctl.set(pinchXform(s.pinch, distance(a, b), midpoint(a, b), geo.frame, geo.content));
    } else if (s.pan) {
      if (s.moved) s.acted = true;
      ctl.set(clampPan({ s: s.pan.xf.s, x: s.pan.xf.x + p.x - s.pan.from.x, y: s.pan.xf.y + p.y - s.pan.from.y }, geo.frame, geo.content));
    }
  };
  const onPointerUp = (e: React.PointerEvent) => {
    const s = g.current;
    if (!s.pointers.delete(e.pointerId)) return;
    if (s.pinch && s.pointers.size < 2) {
      s.pinch = null;
      // one finger left on the glass: carry on panning from where it is, without a jump
      const rest = [...s.pointers.values()][0];
      if (rest && isZoomed(ctl.xf)) s.pan = { from: rest, xf: ctl.xf };
    }
    if (s.pointers.size > 0) return;
    endGesture();
    // double-tap (touch) resets; a mouse double-click arrives as dblclick
    const d = s.down;
    if (e.type === "pointerup" && d?.touch && !s.moved && Date.now() - d.at < TAP_MS) {
      const p = local(e);
      const last = s.lastTap;
      if (last && Date.now() - last.at < TAP_MS && Math.hypot(p.x - last.p.x, p.y - last.p.y) < TAP_PX && opts.current.dblReset && isZoomed(ctl.xf)) {
        ctl.reset();
        s.lastTap = null;
      } else s.lastTap = { at: Date.now(), p };
    } else s.lastTap = null;
  };

  // The phone Live view switches cameras on a one-finger swipe (touch events on the tile). A pinch, or any touch
  // while zoomed, is a zoom/pan gesture: keep it from the swipe handler.
  const onTouch = (e: React.TouchEvent) => {
    const s = g.current;
    if (opts.current.disabled) return;
    if (e.type === "touchstart" && (isZoomed(ctl.xf) || e.touches.length > 1)) s.touchOurs = true;
    if (e.type === "touchmove" && e.touches.length > 1) s.touchOurs = true;
    if (s.touchOurs) e.stopPropagation();
    if ((e.type === "touchend" || e.type === "touchcancel") && e.touches.length === 0) s.touchOurs = false;
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (!keyboard || opts.current.disabled || e.ctrlKey || e.metaKey || e.altKey) return;
    const zoomed = isZoomed(ctl.xf);
    const pan: Record<string, [number, number]> = { ArrowLeft: [40, 0], ArrowRight: [-40, 0], ArrowUp: [0, 40], ArrowDown: [0, -40] };
    if (e.key === "+" || e.key === "=") ctl.zoomTo(ctl.xf.s * STEP, undefined, true);
    else if (e.key === "-" || e.key === "_") ctl.zoomTo(ctl.xf.s / STEP, undefined, true);
    else if (e.key === "0" && zoomed) ctl.reset();
    else if (pan[e.key] && zoomed) ctl.panBy(pan[e.key][0], pan[e.key][1], true);
    else return;
    e.preventDefault();
    e.stopPropagation();
  };

  const stop = (e: React.SyntheticEvent) => e.stopPropagation();
  const hint = dblReset ? "wheel or pinch to zoom, drag to move, double-click to reset" : "wheel or pinch to zoom, drag to move";
  return (
    <div ref={frame} className="vzoom" role="group" tabIndex={keyboard && !disabled ? 0 : undefined}
      aria-label={keyboard && !disabled ? "Video: + and − zoom, arrow keys move, 0 resets" : undefined}
      onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={onPointerUp} onPointerCancel={onPointerUp}
      onPointerLeave={(e) => { if (!frame.current?.hasPointerCapture(e.pointerId)) onPointerUp(e); }}
      onTouchStart={onTouch} onTouchMove={onTouch} onTouchEnd={onTouch} onTouchCancel={onTouch}
      onClickCapture={(e) => { if (g.current.acted) e.stopPropagation(); }}   // the end of a pan or pinch is not a click
      onDoubleClick={(e) => { if (dblReset && !disabled && isZoomed(ctl.xf)) { e.stopPropagation(); ctl.reset(); } }}
      onKeyDown={onKeyDown}>
      <div ref={inner} className="vzoom-inner">{children}</div>
      {view.zoomed && !disabled && (
        <div className="vzoom-pill" title={`Digital zoom: ${hint}`} onPointerDown={stop} onClick={stop} onDoubleClick={stop}>
          <span aria-live="polite">{view.label}</span>
          <button type="button" onClick={() => ctl.reset()} title="Back to 1×" aria-label="Reset zoom">Reset</button>
        </div>
      )}
    </div>
  );
}
