/**
 * A paused tile's still, sized to the tile and with its age (docs/live-views-spec.md, "Tile tiers"): useStill's timing,
 * the frame fetched rather than put in an <img> so the server's X-Frame-Time (when the frame was recorded; the hub's
 * proxy passes it) gives the tile an honest age. A server reached directly on another origin that won't let the page
 * read the answer falls back to a plain <img> with no age. A still that fails keeps the last one on screen; its age keeps
 * growing, which says more than a blank tile.
 */
import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { STALE_STILL_S, stillAgeText, stillWidth } from "./liveFallback";
import { useStill } from "./useStill";

export type StillFrame = {
  /** what to show (an object URL, or the plain URL in fallback); null until the first frame */
  src: string | null;
  /** when the frame shown was recorded (server clock, seconds); null = unknown (fallback, older server) */
  frameTs: number | null;
  /** the latest attempt failed (no recording, server not answering) */
  failed: boolean;
  /** fallback only: wire to the <img> */
  onLoad?: () => void;
  onError?: () => void;
};

const crossOrigin = (url: string) => { try { return new URL(url, location.href).origin !== location.origin; } catch { return false; } };

export function useStillFrame(active: boolean, urlFor: (ts: number) => string): StillFrame {
  const still = useStill(active);
  const url = urlFor(still.ts);
  const [frame, setFrame] = useState<{ src: string; frameTs: number | null } | null>(null);
  const [plain, setPlain] = useState(false);
  const objectUrl = useRef<string | null>(null);
  const { onLoad, onError } = still;
  const cb = useRef({ onLoad, onError });
  cb.current = { onLoad, onError };
  useEffect(() => {
    if (!active || plain) return;
    const ctl = new AbortController();
    fetch(url, { signal: ctl.signal, cache: "no-store" })
      .then(async (r) => {
        if (!r.ok) { cb.current.onError(); return; }
        const t = Number(r.headers.get("x-frame-time"));
        const src = URL.createObjectURL(await r.blob());
        if (ctl.signal.aborted) { URL.revokeObjectURL(src); return; }
        if (objectUrl.current) URL.revokeObjectURL(objectUrl.current);
        objectUrl.current = src;
        setFrame({ src, frameTs: Number.isFinite(t) && t > 0 ? t : null });
        cb.current.onLoad();
      })
      .catch(() => {
        if (ctl.signal.aborted) return;
        // another origin that won't share the answer: show it the old way; same origin: the server didn't answer
        if (crossOrigin(url)) setPlain(true);
        else cb.current.onError();
      });
    return () => ctl.abort();
  }, [url, active, plain]);
  useEffect(() => () => { if (objectUrl.current) URL.revokeObjectURL(objectUrl.current); }, []);
  if (plain) return { src: url, frameTs: null, failed: still.failed, onLoad, onError };
  return { src: frame?.src ?? null, frameTs: frame?.frameTs ?? null, failed: still.failed };
}

/** The width to ask stills for, from the tile's width on screen (stillWidth: 320 or 640). */
export function useStillWidth(box: React.RefObject<HTMLElement | null>): 320 | 640 {
  const [w, setW] = useState<320 | 640>(640);
  useEffect(() => {
    const el = box.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(([e]) => setW(stillWidth(e.contentRect.width, window.devicePixelRatio || 1)));
    ro.observe(el);
    return () => ro.disconnect();
  }, [box]);
  return w;
}

// one shared 1 s clock for every age on the page, rather than a timer per tile
const clock = (() => {
  let now = Date.now() / 1000;
  const subs = new Set<() => void>();
  let timer: ReturnType<typeof setInterval> | null = null;
  return {
    get: () => now,
    subscribe(f: () => void) {
      subs.add(f);
      if (!timer) timer = setInterval(() => { now = Date.now() / 1000; subs.forEach((s) => s()); }, 1000);
      return () => { subs.delete(f); if (!subs.size && timer) { clearInterval(timer); timer = null; } };
    },
  };
})();
const useNowS = () => useSyncExternalStore(clock.subscribe, clock.get);

/** "still · 8 s" for the tile's bar; "paused" when the frame's time isn't known. */
export function StillAge({ frameTs }: { frameTs: number | null }) {
  if (frameTs == null) return <span className="muted small tile-state" title="Paused: a still frame (the page plays a limited number of cameras at once)">paused</span>;
  return <Age frameTs={frameTs} />;
}
function Age({ frameTs }: { frameTs: number }) {
  const now = useNowS();
  return <span className="muted small tile-state" title="Paused: a still frame, this old (the page plays a limited number of cameras at once)">
    still · {stillAgeText(now - frameTs)}</span>;
}

/** On the picture, once the still is older than STALE_STILL_S: the frame shown is no longer a fair picture of now. */
export function StaleStillBadge({ frameTs }: { frameTs: number | null }) {
  return frameTs == null ? null : <Stale frameTs={frameTs} />;
}
function Stale({ frameTs }: { frameTs: number }) {
  const now = useNowS();
  if (now - frameTs < STALE_STILL_S) return null;
  return <div className="player-badge stale" role="status">Still from {stillAgeText(now - frameTs)} ago</div>;
}
