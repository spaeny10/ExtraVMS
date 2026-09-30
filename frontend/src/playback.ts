import { useCallback, useEffect, useRef, useState } from "react";
import { frameUrl } from "./api";

export const CHUNK = 600; // seconds of recording loaded per playback request near live
export const FAR_CHUNK = 120; // ...and when scrubbing back in time: several cameras each pulling a 10-minute file over a remote link is what buffers
export const FIRST_FAR_CHUNK = 20; // the first chunk after a far-back seek: small, so playback starts within a second; the next one is prefetched
export const PREFETCH_LEAD_S = 15; // start fetching the next chunk this long before the current one ends
export const FAR_S = 1800;
export const isFar = (t: number): boolean => nowS() - t >= FAR_S;
/** How long a playback chunk starting at `t` should be. */
export const chunkLen = (t: number): number => (isFar(t) ? FAR_CHUNK : CHUNK);
/** ...and for the first chunk after a seek: a short one far back, so the tile shows video quickly while the
 *  full-length continuation downloads behind it. */
export const firstChunkLen = (t: number): number => (isFar(t) ? FIRST_FAR_CHUNK : CHUNK);

export type Prefetched = { start: number; len: number; url: string | null; ctrl: AbortController; done: boolean };

/** Download the chunk at `start` into memory (a blob URL) so the next <video> starts from cache, not the network. */
export function prefetchChunk(fetchUrl: string, start: number, len: number): Prefetched {
  const ctrl = new AbortController();
  const p: Prefetched = { start, len, url: null, ctrl, done: false };
  fetch(fetchUrl, { signal: ctrl.signal })
    .then((r) => (r.ok ? r.blob() : null))
    .then((b) => { if (b && !ctrl.signal.aborted) p.url = URL.createObjectURL(b); })
    .catch(() => { /* aborted or offline: the tile falls back to a network URL */ })
    .finally(() => { p.done = true; });
  return p;
}
export function dropPrefetch(p: Prefetched | null): void {
  if (!p) return;
  if (!p.done) p.ctrl.abort();
  if (p.url) URL.revokeObjectURL(p.url);
}
export const LIVE_LAG = 3; // MediaMTX flushes fMP4 parts every second; stay a little behind "now"

export type Span = { start: number; end: number };

export const nowS = () => Date.now() / 1000;
const pad = (n: number) => String(n).padStart(2, "0");

export function fmtClock(t: number): string {
  const d = new Date(t * 1000);
  return `${d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" })} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

export const spanAt = (spans: Span[] | undefined, t: number) => spans?.find((s) => t >= s.start && t < s.end);

/** True if clip time `rel` (seconds) is already buffered, so a seek there will succeed. */
export function isBuffered(v: HTMLVideoElement, rel: number, margin = 0.3): boolean {
  for (let i = 0; i < v.buffered.length; i++) {
    if (rel >= v.buffered.start(i) && rel <= v.buffered.end(i) - margin) return true;
  }
  return false;
}

export type FrameShot = { url: string | null; t: number | null; cam: string };

/** Preview frames from the recordings with at most one request in flight; the newest requested time wins. */
export function useLatestFrame(width: number) {
  const [shot, setShot] = useState<FrameShot | null>(null);
  const wanted = useRef<{ cam: string; t: number; exact: boolean } | null>(null);
  const last = useRef<string>("");
  const inflight = useRef(false);
  const gen = useRef(0); // bumped by clear() so late responses are dropped
  const urlRef = useRef<string | null>(null);

  const pump = useCallback(async () => {
    if (inflight.current || !wanted.current) return;
    const req = wanted.current;
    const g = gen.current;
    inflight.current = true;
    try {
      const r = await fetch(frameUrl(req.cam, req.t, width, req.exact));
      const blob = r.ok ? await r.blob() : null;
      if (g === gen.current) {
        const url = blob ? URL.createObjectURL(blob) : null;
        if (urlRef.current) URL.revokeObjectURL(urlRef.current);
        urlRef.current = url;
        setShot({ url, t: blob ? Number(r.headers.get("X-Frame-Time")) || req.t : null, cam: req.cam });
      }
    } catch {
      /* transient; the next move retries */
    } finally {
      inflight.current = false;
    }
    if (wanted.current && wanted.current !== req) pump();
  }, [width]);

  const request = useCallback((cam: string, t: number, exact = false) => {
    const key = `${cam}|${t.toFixed(2)}|${exact}`;
    if (key === last.current && wanted.current) return;
    last.current = key;
    wanted.current = { cam, t, exact };
    pump();
  }, [pump]);
  /** Stop requesting but keep showing the last frame. */
  const stop = useCallback(() => {
    wanted.current = null;
    last.current = "";
  }, []);
  /** Stop and hide. */
  const clear = useCallback(() => {
    wanted.current = null;
    last.current = "";
    gen.current++;
    if (urlRef.current) URL.revokeObjectURL(urlRef.current);
    urlRef.current = null;
    setShot(null);
  }, []);
  useEffect(() => () => {
    if (urlRef.current) URL.revokeObjectURL(urlRef.current);
  }, []);
  return { shot, request, stop, clear };
}
