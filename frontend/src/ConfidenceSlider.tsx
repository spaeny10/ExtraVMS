import { useEffect, useRef, useState } from "react";

/** Minimum YOLO confidence filter (0 = any). Debounced so dragging doesn't spam queries. */
export function ConfidenceSlider({ value, onChange }: { value: number; onChange: (v: number) => void }) {
  const [local, setLocal] = useState(value);
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => setLocal(value), [value]);
  useEffect(() => () => clearTimeout(timer.current), []);
  const set = (v: number) => {
    setLocal(v);
    clearTimeout(timer.current);
    timer.current = window.setTimeout(() => onChange(v), 250);
  };
  const pct = Math.round(local * 100);
  return (
    <label className="conf-slider" title="Hide events YOLO verified below this confidence. Events still being verified are always shown.">
      <span className="muted small">Min YOLO</span>
      <input type="range" min={0} max={95} step={5} value={pct} onChange={(e) => set(Number(e.target.value) / 100)}
        style={{ ["--fill" as string]: `${(pct / 95) * 100}%` }} />
      <span className="conf-val small">{pct > 0 ? `${pct}%` : "Any"}</span>
    </label>
  );
}

export function loadNumber(key: string, fallback = 0): number {
  try {
    const v = Number(localStorage.getItem(key));
    return Number.isFinite(v) && localStorage.getItem(key) !== null ? v : fallback;
  } catch {
    return fallback;
  }
}

export function saveNumber(key: string, v: number): void {
  try {
    localStorage.setItem(key, String(v));
  } catch {
    /* private mode */
  }
}
