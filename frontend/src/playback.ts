import { useCallback, useEffect, useRef, useState } from "react";
import { frameUrl } from "./api";

export const CHUNK = 600; // seconds of recording loaded per playback request near live
export const FAR_CHUNK = 120; // ...and when scrubbing back in time: several cameras each pulling a 10-minute file over a remote link is what buffers
export const FIRST_FAR_CHUNK = 20; // the first chunk after a far-back seek: small, so playback starts within a second; the next one is prefetched
export const PREFETCH_LEAD_S = 15; // start fetching the next chunk this long before the current one ends
export const FAR_S = 1800;
export const isFar = (t: number): boolean => nowS() - t >= FAR_S;
export const REMOTE_FIRST_CHUNK = 20; // through the hub: the site uplink is slow, so start with a chunk that arrives in a few seconds
export const REMOTE_CHUNK = 60; // ...and continue in 60 s chunks (near live too): a 600 s HEVC chunk can't arrive faster than real time
/** How long a playback chunk starting at `t` should be (`remote`: the camera is reached through the hub). */
export const chunkLen = (t: number, remote = false): number => (remote ? REMOTE_CHUNK : isFar(t) ? FAR_CHUNK : CHUNK);
/** ...and for the first chunk after a seek: a short one far back (or remote), so the tile shows video quickly while
 *  the full-length continuation downloads behind it. */
export const firstChunkLen = (t: number, remote = false): number => (remote ? REMOTE_FIRST_CHUNK : isFar(t) ? FIRST_FAR_CHUNK : CHUNK);

export const STALL_S = 8; // remote: a download whose buffer hasn't grown (and video hasn't moved) for this long is dead
export const AHEAD_SLACK_S = 4; // the clock may run this far past the buffered end before the tile considers reloading
/** Minimum seconds between drift-triggered reloads of one tile, so a slow link can't turn into a request storm. */
export const DRIFT_RELOAD_MIN_S = { local: 5, remote: 15 } as const;
/** Longest the shared clock holds for a buffering tile: remote links need longer, but one dead camera must not freeze the rest. */
export const HOLD_MAX_MS = { local: 3000, remote: 20000 } as const;

/**
 * Should a tile whose shared clock is past its buffered video drop the chunk and fetch a new one at the clock?
 * Local: yes once more than AHEAD_SLACK_S past (today's behaviour). Remote: only when waiting is pointless — the
 * user seeked (`jumped`), the clock is more than 2 chunk lengths past, or nothing has progressed for STALL_S;
 * otherwise the download is still arriving and a reload would only abort it and start over.
 * `pastChunkEnd`: the clock is already beyond this chunk, so any gap counts (the slack doesn't apply).
 * Times are seconds; `clockRel`/`bufferedEnd` are relative to the chunk start, `now`/`lastProgressAt` any one clock.
 */
export function shouldReload(o: {
  clockRel: number; bufferedEnd: number; lastProgressAt: number; now: number; remote: boolean;
  len?: number; jumped?: boolean; pastChunkEnd?: boolean;
}): boolean {
  const gap = o.clockRel - o.bufferedEnd;
  if (!o.pastChunkEnd && gap <= AHEAD_SLACK_S) return false;
  if (!o.remote || o.jumped) return true;
  if (o.len != null && gap > 2 * o.len) return true;
  return o.now - o.lastProgressAt >= STALL_S;
}
/** May a tile reload because of drift now? (`lastAt` = its previous drift reload, seconds, or null.) */
export const driftReloadAllowed = (lastAt: number | null, now: number, remote: boolean): boolean =>
  lastAt == null || now - lastAt >= (remote ? DRIFT_RELOAD_MIN_S.remote : DRIFT_RELOAD_MIN_S.local);

export const RETRY_BACKOFF_MS = [2000, 5000, 10000]; // recordings retry after a failure: quick at first...
export const RETRY_STEADY_MS = 30000; // ...then every 30 s until the server answers
/** Delay before retry number `attempt` (0-based) of a failed recordings load. */
export const retryDelayMs = (attempt: number): number => RETRY_BACKOFF_MS[attempt] ?? RETRY_STEADY_MS;

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
/** Will the clock at `t`, advancing by `dt` seconds at `speed`, reach the recording's live edge (now - LIVE_LAG)?
 *  Then the Timeline switches to the live streams rather than reloading chunks that run dry. Rewinding or slow
 *  motion never gets there. */
export const reachesLiveEdge = (t: number, dt: number, speed: number, now: number): boolean => speed >= 1 && t + dt * speed >= now - LIVE_LAG;

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

/** Lane key for a camera on a server; this server's cameras ("" server) keep their bare id, so saved state still matches. */
export const camKey = (server: string, id: string): string => (server ? `${server}/${id}` : id);
/** Inverse of camKey: a key without "/" is a camera on this server. */
export function splitKey(key: string): { server: string; id: string } {
  const i = key.indexOf("/");
  return i < 0 ? { server: "", id: key } : { server: key.slice(0, i), id: key.slice(i + 1) };
}

export type FrameShot = { url: string | null; t: number | null; cam: string };

/** Preview frames from the recordings with at most one request in flight; the newest requested time wins. */
export type FrameUrlFor = (cam: string, t: number, w?: number, exact?: boolean) => string;

/** `frameUrlFor` maps the requested cam (a lane key on the hub) to a URL; it must be stable (memoised). */
export function useLatestFrame(width: number, frameUrlFor: FrameUrlFor = frameUrl) {
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
      const r = await fetch(frameUrlFor(req.cam, req.t, width, req.exact));
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
