/**
 * The widget grid: CSS grid placement plus, in edit mode, pointer drag (header) and resize (corner) with
 * snapping and push-down (grid.ts). Phones get a single column and no editing.
 */
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { clampBox, resolve, stackForPhone } from "./grid";
import type { AnyWidget, DashboardConfig } from "./types";

const GAP = 10;
const DRAG_PX = 6;

export function usePhone(): boolean {
  const [phone, setPhone] = useState(() => typeof matchMedia !== "undefined" && matchMedia("(max-width: 700px)").matches);
  useEffect(() => {
    const mq = matchMedia("(max-width: 700px)");
    const on = () => setPhone(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  return phone;
}

export function DashboardGrid({ config, editing, onChange, render, minSize }: {
  config: DashboardConfig; editing: boolean; onChange: (c: DashboardConfig) => void;
  render: (w: AnyWidget, dragHandle: (e: React.PointerEvent) => void) => ReactNode;
  minSize: (w: AnyWidget) => { w: number; h: number };
}) {
  const phone = usePhone();
  const host = useRef<HTMLDivElement>(null);
  const [live, setLive] = useState<AnyWidget[] | null>(null);   // layout while a drag/resize is in flight
  const [activeId, setActiveId] = useState<string | null>(null);
  const [mode, setMode] = useState<"move" | "resize" | null>(null);
  const liveRef = useRef<AnyWidget[] | null>(null);
  useLayoutEffect(() => { liveRef.current = live; }, [live]);

  const cols = config.cols, rowH = config.rowH;
  const widgets = live ?? config.widgets;
  const shown = phone ? stackForPhone(widgets, cols) : widgets;
  const rows = shown.reduce((m, w) => Math.max(m, w.y + w.h), 1);

  /** pixels → grid cells, from the rendered column width */
  const metrics = () => {
    const el = host.current!;
    const colW = (el.clientWidth - GAP * (cols - 1)) / cols;
    return { colW: colW + GAP, rowStep: rowH + GAP };
  };

  const start = (id: string, kind: "move" | "resize") => (e: React.PointerEvent) => {
    if (!editing || phone) return;
    if (e.pointerType === "mouse" && e.button !== 0) return;
    if ((e.target as HTMLElement).closest("button, select, input, a")) return;
    e.preventDefault();
    const origin = config.widgets.find((w) => w.id === id)!;
    const min = minSize(origin);
    const x0 = e.clientX, y0 = e.clientY;
    let started = false;
    const m = metrics();
    const move = (ev: PointerEvent) => {
      const dx = ev.clientX - x0, dy = ev.clientY - y0;
      if (!started && Math.hypot(dx, dy) < DRAG_PX) return;
      if (!started) { started = true; setActiveId(id); setMode(kind); }
      ev.preventDefault();
      const cx = Math.round(dx / m.colW), cy = Math.round(dy / m.rowStep);
      const moved = kind === "move"
        ? clampBox({ ...origin, x: origin.x + cx, y: origin.y + cy }, cols, min.w, min.h)
        : clampBox({ ...origin, w: origin.w + cx, h: origin.h + cy }, cols, min.w, min.h);
      const next = resolve(config.widgets.map((w) => (w.id === id ? (moved as AnyWidget) : w)), id);
      setLive(next);
    };
    const end = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", end);
      window.removeEventListener("pointercancel", end);
      const final = liveRef.current;
      setLive(null); setActiveId(null); setMode(null);
      if (started && final) onChange({ ...config, widgets: final });
    };
    window.addEventListener("pointermove", move, { passive: false });
    window.addEventListener("pointerup", end);
    window.addEventListener("pointercancel", end);
  };

  return (
    <div ref={host} className={`dash-grid ${editing && !phone ? "editing" : ""} ${phone ? "phone" : ""}`}
      style={{ gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))`, gridAutoRows: `${rowH}px`, gap: GAP, minHeight: rows * (rowH + GAP) }}>
      {shown.map((w) => (
        <div key={w.id} className={`dash-item ${activeId === w.id ? `active ${mode}` : ""}`} data-wid={w.id}
          style={{ gridColumn: `${w.x + 1} / span ${w.w}`, gridRow: `${w.y + 1} / span ${w.h}` }}>
          {render(w, start(w.id, "move"))}
          {editing && !phone && <div className="dash-resize" title="Drag to resize" onPointerDown={start(w.id, "resize")} />}
        </div>
      ))}
    </div>
  );
}
