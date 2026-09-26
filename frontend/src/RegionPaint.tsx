/**
 * Paint-a-region overlay for a camera tile (Live and Timeline): a canvas over the video's content rect where
 * the user brushes grid cells; the painted cells become that camera's event filter (region.ts).
 */
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { Camera } from "./api";
import { GRID_H, GRID_W, cellIndex, countCells, hasCell, isEmpty, regions, saveAsPlace, setCell, useRegion } from "./region";

const COARSE = typeof matchMedia !== "undefined" && matchMedia("(pointer: coarse)").matches;
type Rect = { left: number; top: number; width: number; height: number };

/** Where the video's picture sits inside its box (object-fit: contain). */
export function contentRect(box: { width: number; height: number }, aspect: number): Rect {
  const boxAspect = box.width / Math.max(1, box.height);
  if (boxAspect > aspect) { const w = box.height * aspect; return { left: (box.width - w) / 2, top: 0, width: w, height: box.height }; }
  const h = box.width / aspect;
  return { left: 0, top: (box.height - h) / 2, width: box.width, height: h };
}

export function RegionOverlay({ cam, videoRef, editing, onDone, camera, fallbackAspect = 2592 / 1520 }: {
  cam: string; videoRef: React.RefObject<HTMLVideoElement | null>; editing: boolean; onDone: () => void;
  camera?: Camera; fallbackAspect?: number;
}) {
  const stored = useRegion(cam);
  const canvas = useRef<HTMLCanvasElement>(null);
  const [rect, setRect] = useState<Rect | null>(null);
  const [bits, setBits] = useState<Uint8Array>(() => stored ? new Uint8Array(stored) : new Uint8Array(GRID_W * GRID_H / 8));
  const [mode, setMode] = useState<"paint" | "erase">("paint");
  const [brush, setBrush] = useState(COARSE ? 2 : 1);
  const stroke = useRef<{ erase: boolean } | null>(null);
  const bitsRef = useRef(bits);  // pointer events arrive faster than renders: paint on the latest bits
  bitsRef.current = bits;

  useEffect(() => { if (!editing) setBits(stored ? new Uint8Array(stored) : new Uint8Array(GRID_W * GRID_H / 8)); }, [stored, editing]);

  // follow the tile size and the stream's real aspect ratio
  useLayoutEffect(() => {
    const el = canvas.current?.parentElement;
    if (!el) return;
    const measure = () => {
      const v = videoRef.current;
      const aspect = v && v.videoWidth && v.videoHeight ? v.videoWidth / v.videoHeight : fallbackAspect;
      setRect(contentRect({ width: el.clientWidth, height: el.clientHeight }, aspect));
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    const v = videoRef.current;
    v?.addEventListener("loadedmetadata", measure);
    v?.addEventListener("resize", measure);
    return () => { ro.disconnect(); v?.removeEventListener("loadedmetadata", measure); v?.removeEventListener("resize", measure); };
  }, [videoRef, fallbackAspect, editing]);

  // draw
  useEffect(() => {
    const c = canvas.current;
    if (!c || !rect) return;
    const dpr = window.devicePixelRatio || 1;
    c.width = Math.round(rect.width * dpr); c.height = Math.round(rect.height * dpr);
    const g = c.getContext("2d");
    if (!g) return;
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, rect.width, rect.height);
    const cw = rect.width / GRID_W, ch = rect.height / GRID_H;
    const accent = getComputedStyle(c).getPropertyValue("--accent").trim() || "#4c8dff";
    if (editing) {
      g.strokeStyle = "rgba(255,255,255,.18)"; g.lineWidth = 1;
      for (let k = 1; k < GRID_W; k++) { g.beginPath(); g.moveTo(k * cw, 0); g.lineTo(k * cw, rect.height); g.stroke(); }
      for (let r = 1; r < GRID_H; r++) { g.beginPath(); g.moveTo(0, r * ch); g.lineTo(rect.width, r * ch); g.stroke(); }
    }
    g.fillStyle = accent; g.globalAlpha = editing ? 0.38 : 0.22;
    for (let r = 0; r < GRID_H; r++) for (let k = 0; k < GRID_W; k++) if (hasCell(bits, cellIndex(k, r))) g.fillRect(k * cw, r * ch, cw + 0.5, ch + 0.5);
    g.globalAlpha = 1;
    if (!editing) { // outline so the region reads on light video
      g.strokeStyle = accent; g.lineWidth = 1.5;
      for (let r = 0; r < GRID_H; r++) for (let k = 0; k < GRID_W; k++) {
        const i = cellIndex(k, r);
        if (!hasCell(bits, i)) continue;
        if (r === 0 || !hasCell(bits, cellIndex(k, r - 1))) { g.beginPath(); g.moveTo(k * cw, r * ch); g.lineTo((k + 1) * cw, r * ch); g.stroke(); }
        if (r === GRID_H - 1 || !hasCell(bits, cellIndex(k, r + 1))) { g.beginPath(); g.moveTo(k * cw, (r + 1) * ch); g.lineTo((k + 1) * cw, (r + 1) * ch); g.stroke(); }
        if (k === 0 || !hasCell(bits, cellIndex(k - 1, r))) { g.beginPath(); g.moveTo(k * cw, r * ch); g.lineTo(k * cw, (r + 1) * ch); g.stroke(); }
        if (k === GRID_W - 1 || !hasCell(bits, cellIndex(k + 1, r))) { g.beginPath(); g.moveTo((k + 1) * cw, r * ch); g.lineTo((k + 1) * cw, (r + 1) * ch); g.stroke(); }
      }
    }
  }, [bits, rect, editing]);

  if (!editing && (!stored || isEmpty(stored))) return null;

  const paintAt = (e: React.PointerEvent, erase: boolean) => {
    const c = canvas.current;
    if (!c || !rect) return;
    const r = c.getBoundingClientRect();
    const col = Math.floor(((e.clientX - r.left) / r.width) * GRID_W), row = Math.floor(((e.clientY - r.top) / r.height) * GRID_H);
    const next = new Uint8Array(bitsRef.current);
    const rad = brush - 1;
    for (let dr = -rad; dr <= rad; dr++) for (let dk = -rad; dk <= rad; dk++) {
      const rr = row + dr, kk = col + dk;
      if (rr >= 0 && rr < GRID_H && kk >= 0 && kk < GRID_W) setCell(next, cellIndex(kk, rr), !erase);
    }
    bitsRef.current = next;
    setBits(next);
    return next;
  };
  const commit = (b: Uint8Array) => regions.set(cam, isEmpty(b) ? null : b);
  const stop = (e: React.SyntheticEvent) => { e.stopPropagation(); };

  return (
    <>
      <canvas ref={canvas} className={`region-canvas ${editing ? "editing" : ""}`}
        style={rect ? { left: rect.left, top: rect.top, width: rect.width, height: rect.height } : undefined}
        onClick={stop} onDoubleClick={stop} onContextMenu={(e) => { e.preventDefault(); e.stopPropagation(); }}
        onPointerDown={(e) => {
          if (!editing) return;
          e.stopPropagation(); e.preventDefault();
          e.currentTarget.setPointerCapture(e.pointerId);
          const erase = mode === "erase" || e.button === 2 || e.altKey;
          stroke.current = { erase };
          paintAt(e, erase);
        }}
        onPointerMove={(e) => { if (stroke.current) { e.preventDefault(); paintAt(e, stroke.current.erase); } }}
        onPointerUp={(e) => { if (stroke.current) { const erase = stroke.current.erase; stroke.current = null; commit(paintAt(e, erase) ?? bitsRef.current); } }}
        onPointerCancel={() => { if (stroke.current) { stroke.current = null; commit(bitsRef.current); } }}
      />
      {editing && (
        <div className="region-tools" onPointerDown={stop} onClick={stop} onDoubleClick={stop}>
          <div className="segmented small-seg">
            <button className={mode === "paint" ? "active" : ""} onClick={() => setMode("paint")} title="Paint cells (drag)">✎ Paint</button>
            <button className={mode === "erase" ? "active" : ""} onClick={() => setMode("erase")} title="Erase cells (or right-drag / Alt-drag)">Erase</button>
          </div>
          <div className="segmented small-seg" title="Brush size">
            {[1, 2, 3].map((b) => <button key={b} className={brush === b ? "active" : ""} onClick={() => setBrush(b)}>{b}</button>)}
          </div>
          <span className="muted-on-dark">{countCells(bits)} cells</span>
          <button className="ghost small" disabled={isEmpty(bits)} onClick={() => { const b = new Uint8Array(bits.length); setBits(b); commit(b); }}>Clear</button>
          {camera && <button className="ghost small" disabled={isEmpty(bits)} title="Turn the painted cells into a named place (zone) on this camera"
            onClick={() => saveAsPlace(camera, bits)}>Save as named place…</button>}
          <button className="small" onClick={onDone}>Done</button>
        </div>
      )}
    </>
  );
}

/** Small chip for a tile bar: the region is on; ✕ clears it. */
export function RegionBadge({ cam, onEdit }: { cam: string; onEdit?: () => void }) {
  const r = useRegion(cam);
  if (!r) return null;
  return (
    <span className="region-badge" title="Only events that passed through the painted region are shown for this camera" onClick={(e) => e.stopPropagation()}>
      <button className="linkish" onClick={onEdit}>▦ region</button>
      <button className="linkish" aria-label="Clear region" onClick={() => regions.set(cam, null)}>✕</button>
    </span>
  );
}
