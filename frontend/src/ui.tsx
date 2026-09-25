/* Small UI toolkit: SVG icons, toasts, in-app confirm/prompt dialogs, skeletons, connection status. */
import { useEffect, useState, useSyncExternalStore } from "react";

// ---------------------------------------------------------------- icons (24px stroke paths, Lucide-style)

const PATHS: Record<string, string> = {
  home: "M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z",
  live: "M4 7a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2zM17 10l4-2.5v9L17 14",
  events: "M4 6h16M4 12h16M4 18h10",
  find: "M11 4a7 7 0 1 1 0 14 7 7 0 0 1 0-14zM20 20l-4-4",
  timeline: "M3 12h18M7 8v8M12 6v12M17 9v6",
  settings: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z",
  camera: "M3 8a2 2 0 0 1 2-2h2l1.5-2h7L17 6h2a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2zM12 17a4 4 0 1 0 0-8 4 4 0 0 0 0 8z",
  x: "M6 6l12 12M18 6 6 18",
  check: "M4 12.5 9.5 18 20 7",
  alert: "M12 3 2 20h20zM12 9v5M12 17.5h.01",
  info: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 11v5M12 8h.01",
  lock: "M6 11V8a6 6 0 0 1 12 0v3M5 11h14v10H5z",
  link: "M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1",
  sparkle: "M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8zM19 16l.8 2.2L22 19l-2.2.8L19 22l-.8-2.2L16 19l2.2-.8z",
  sun: "M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10zM12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4",
  moon: "M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z",
  auto: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 3v18M12 3a9 9 0 0 1 0 18",
  chevronDown: "M6 9l6 6 6-6",
  chevronRight: "M9 6l6 6-6 6",
  chevronLeft: "M15 6l-6 6 6 6",
  play: "M7 5v14l11-7z",
  clock: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 7v5l3 2",
  wifiOff: "M2 2l20 20M8.5 16.5a5 5 0 0 1 7 0M5 13a10 10 0 0 1 3.4-2.3M12 8a14 14 0 0 1 10 4M2 12a14 14 0 0 1 3.1-2.5M12 20h.01",
  refresh: "M21 12a9 9 0 1 1-2.6-6.4M21 4v5h-5",
  user: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM4 21a8 8 0 0 1 16 0",
  car: "M5 17h14M3 12l2-5h14l2 5v6h-2v-2H5v2H3zM7 15h.01M17 15h.01",
  more: "M12 6h.01M12 12h.01M12 18h.01",
  grid: "M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z",
  expand: "M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5",
};

export function Icon({ name, size = 18, className = "" }: { name: keyof typeof PATHS | string; size?: number; className?: string }) {
  return (
    <svg className={`icon ${className}`} width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={PATHS[name] ?? PATHS.info} />
    </svg>
  );
}

// ---------------------------------------------------------------- tiny external store

function makeStore<T>(initial: T) {
  let state = initial;
  const subs = new Set<() => void>();
  return {
    get: () => state,
    set: (next: T) => { state = next; subs.forEach((f) => f()); },
    subscribe: (f: () => void) => { subs.add(f); return () => { subs.delete(f); }; },
  };
}

// ---------------------------------------------------------------- toasts

export type Toast = { id: number; kind: "success" | "error" | "info"; text: string; undo?: () => void; ttl: number };
const toasts = makeStore<Toast[]>([]);
let toastSeq = 0;

function push(kind: Toast["kind"], text: string, opts: { undo?: () => void; ttl?: number } = {}) {
  const t: Toast = { id: ++toastSeq, kind, text, undo: opts.undo, ttl: opts.ttl ?? (kind === "error" ? 8000 : opts.undo ? 7000 : 3500) };
  toasts.set([...toasts.get(), t].slice(-4));
  setTimeout(() => dismiss(t.id), t.ttl);
  return t.id;
}
function dismiss(id: number) { toasts.set(toasts.get().filter((t) => t.id !== id)); }

export const toast = {
  success: (text: string, opts?: { undo?: () => void }) => push("success", text, opts),
  error: (text: unknown) => push("error", errorText(text)),
  info: (text: string) => push("info", text),
};

/** "409 {"detail":"a layout with that name exists"}" -> "a layout with that name exists" */
export function errorText(e: unknown): string {
  const s = String(e instanceof Error ? e.message : e);
  const m = /"detail"\s*:\s*"([^"]+)"/.exec(s);
  if (m) return m[1];
  if (/^Error: 4\d\d|^4\d\d /.test(s)) return s.replace(/^Error:\s*/, "").replace(/^\d{3}\s*/, "") || "Request failed";
  if (/Failed to fetch|NetworkError|Load failed/.test(s)) return "Can't reach the NVR";
  return s.replace(/^Error:\s*/, "");
}

export function Toaster() {
  const list = useSyncExternalStore(toasts.subscribe, toasts.get);
  if (!list.length) return null;
  return (
    <div className="toasts" role="status" aria-live="polite">
      {list.map((t) => (
        <div key={t.id} className={`toast ${t.kind}`}>
          <Icon name={t.kind === "success" ? "check" : t.kind === "error" ? "alert" : "info"} size={16} />
          <span>{t.text}</span>
          {t.undo && <button className="linkish" onClick={() => { t.undo?.(); dismiss(t.id); }}>Undo</button>}
          <button className="toast-x" aria-label="Dismiss" onClick={() => dismiss(t.id)}><Icon name="x" size={14} /></button>
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------- dialogs (replace window.confirm / prompt)

type DialogReq =
  | { kind: "confirm"; title: string; message?: string; confirmLabel: string; danger: boolean; resolve: (ok: boolean) => void }
  | { kind: "prompt"; title: string; message?: string; label: string; initial: string; confirmLabel: string; resolve: (v: string | null) => void };
const dialog = makeStore<DialogReq | null>(null);

export function confirmDialog(title: string, opts: { message?: string; confirmLabel?: string; danger?: boolean } = {}): Promise<boolean> {
  return new Promise((resolve) => dialog.set({ kind: "confirm", title, message: opts.message, confirmLabel: opts.confirmLabel ?? "OK", danger: !!opts.danger, resolve }));
}
export function promptDialog(title: string, opts: { message?: string; label?: string; initial?: string; confirmLabel?: string } = {}): Promise<string | null> {
  return new Promise((resolve) => dialog.set({ kind: "prompt", title, message: opts.message, label: opts.label ?? "", initial: opts.initial ?? "", confirmLabel: opts.confirmLabel ?? "Save", resolve }));
}

export function Dialogs() {
  const d = useSyncExternalStore(dialog.subscribe, dialog.get);
  const [value, setValue] = useState("");
  useEffect(() => { if (d?.kind === "prompt") setValue(d.initial); }, [d]);
  useEffect(() => {
    if (!d) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") close(null); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });
  if (!d) return null;
  const close = (result: boolean | string | null) => {
    dialog.set(null);
    if (d.kind === "confirm") d.resolve(Boolean(result));
    else d.resolve(typeof result === "string" ? result : null);
  };
  return (
    <div className="modal-backdrop dialog-backdrop" onClick={() => close(null)}>
      <form className="dialog" onClick={(e) => e.stopPropagation()} onSubmit={(e) => { e.preventDefault(); close(d.kind === "prompt" ? value.trim() : true); }}>
        <h3>{d.title}</h3>
        {d.message && <p className="muted">{d.message}</p>}
        {d.kind === "prompt" && (
          <label className="field">{d.label && <span>{d.label}</span>}<input autoFocus value={value} onChange={(e) => setValue(e.target.value)} /></label>
        )}
        <div className="row dialog-actions">
          <button type="button" className="ghost" onClick={() => close(null)}>Cancel</button>
          <button type="submit" className={d.kind === "confirm" && d.danger ? "danger" : ""} autoFocus={d.kind === "confirm"}
            disabled={d.kind === "prompt" && !value.trim()}>{d.confirmLabel}</button>
        </div>
      </form>
    </div>
  );
}

// ---------------------------------------------------------------- loading skeletons

export function Skeleton({ lines = 3, card = false }: { lines?: number; card?: boolean }) {
  return (
    <div className={`skeleton ${card ? "skeleton-card" : ""}`} aria-hidden="true">
      {card && <div className="sk-thumb" />}
      <div className="sk-lines">{Array.from({ length: lines }, (_, i) => <div key={i} className="sk-line" style={{ width: `${90 - i * 18}%` }} />)}</div>
    </div>
  );
}
export function SkeletonGrid({ n = 6 }: { n?: number }) {
  return <div className="event-grid">{Array.from({ length: n }, (_, i) => <Skeleton key={i} card lines={3} />)}</div>;
}

// ---------------------------------------------------------------- connection status

export type Connection = { online: boolean; ws: boolean; lastData: number };
const conn = makeStore<Connection>({ online: typeof navigator === "undefined" ? true : navigator.onLine, ws: false, lastData: Date.now() });
export const connection = {
  ws: (up: boolean) => conn.set({ ...conn.get(), ws: up, lastData: up ? Date.now() : conn.get().lastData }),
  data: () => conn.set({ ...conn.get(), lastData: Date.now() }),
};
if (typeof window !== "undefined") {
  window.addEventListener("online", () => conn.set({ ...conn.get(), online: true }));
  window.addEventListener("offline", () => conn.set({ ...conn.get(), online: false }));
}
export const useConnection = () => useSyncExternalStore(conn.subscribe, conn.get);

/** Banner shown when the NVR can't be reached (browser offline, or the live socket down for a while). */
export function OfflineBanner() {
  const c = useConnection();
  const [, tick] = useState(0);
  useEffect(() => { const t = setInterval(() => tick((x) => x + 1), 5000); return () => clearInterval(t); }, []);
  const down = !c.online || (!c.ws && Date.now() - c.lastData > 8000);
  if (!down) return null;
  const ago = Math.round((Date.now() - c.lastData) / 60000);
  return (
    <div className="offline-banner" role="alert">
      <Icon name="wifiOff" size={16} />
      <span>{c.online ? "Can't reach the NVR" : "You're offline"} · last update {ago < 1 ? "under a minute" : `${ago} min`} ago</span>
      <button className="linkish small" onClick={() => location.reload()}>Retry</button>
    </div>
  );
}
