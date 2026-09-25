import { useCallback, useEffect, useRef, useState } from "react";
import { frameUrl } from "./api";

export const CHUNK = 600; // seconds of recording loaded per playback request
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
