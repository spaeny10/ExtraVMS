import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fmtTime, type NvrEvent, type PlaybackQuality, type SiteApi, type TimelineEvent } from "./api";
import {
  KEY_STEP_S, ZOOM_HALF_S, afterPlaybackError, atFrac, chunkAt, chunkStep, clipOrigin, clock, coverage, defaultQuality, eventEnd,
  followWindow, footageAvailable, initialSource, jumpTarget, listingRange, needsListing, neighbour, nextChunkStart, pauseAtEnd,
  playStart, prefetchLead, relLabel, scaleMarks, sdUsable, snapToRecorded, stripWindow, ticks, toFrac, viaHub, zoomLabel, zoomWindow,
  type Source, type Win,
} from "./eventPlayback";
import { httpStatus } from "./liveFallback";
import { PREFETCH_LEAD_S, dropPrefetch, isBuffered, mergeSpans, nowS, prefetchChunk, spanAt, useLatestFrame, type Prefetched, type Span } from "./playback";
import { Icon, useIsPhone } from "./ui";

/** What the event viewer can do with the player: seek it (Ask answers' frames, notes) to epoch `t`, paused if asked. */
export type EventPlayerHandle = { seek: (t: number, pause?: boolean) => void };

/** Can the server transcode playback to SD (q=sd) quickly (a GPU encoder: sdUsable)? Asked once per server client per
 *  page load; an older server says no. */
const sdCaps = new WeakMap<SiteApi, Promise<boolean>>();
const canSd = (site: SiteApi): Promise<boolean> => {
  let p = sdCaps.get(site);
  if (!p) {
    p = site.playbackCapabilities().then(sdUsable).catch(() => false);
    sdCaps.set(site, p);
  }
  return p;
};

type Listing = { spans: Span[] | null | undefined; events: TimelineEvent[]; from: number; to: number };
/** A recording chunk on screen: `cont` = a continuation (full length), `retry` = how many times it was asked for again,
 *  `asked` = when (performance.now()). */
type Chunk = { start: number; len: number; src: string; key: number; cont: boolean; retry: number; asked: number };
type Stopped = "failed" | "end" | "live" | null;
let chunkSeq = 0;

/** The end of the buffered range at `t` (or `t` itself when nothing is buffered there). */
const bufferedEnd = (v: HTMLVideoElement, t: number): number => {
  for (let i = 0; i < v.buffered.length; i++) if (t >= v.buffered.start(i) - 0.1 && t <= v.buffered.end(i) + 0.1) return v.buffered.end(i);
  return t;
};
/** A <video> that is done with: stop its download now (a streamed SD transcode holds a server slot, and through the hub
 *  a tunnel stream, until the response is dropped; a detached element keeps it until it is garbage collected). */
const release = (v: HTMLVideoElement) => {
  v.pause();
  v.removeAttribute("src");
  v.load();
};

type FsDoc = Document & { webkitFullscreenElement?: Element | null; webkitExitFullscreen?: () => void };
type FsEl = HTMLElement & { webkitRequestFullscreen?: () => void };
const fullscreenEl = (): Element | null => document.fullscreenElement ?? (document as FsDoc).webkitFullscreenElement ?? null;

/**
 * The event viewer's video: the camera's recording from shortly before the event, through it and on, chunk after chunk
 * as the Timeline plays (playback.ts), with a strip under it around the event (±1 min by default; zoom out to ±5 and
 * ±15 min): what is recorded (gaps hatched: older events often keep only the footage retention kept around them), the
 * event, the camera's other events (tap one to go there), a playhead to drag or tap, and previous / next / back to the
 * event. The strip is the timeline: the video has small controls of its own (play / pause, sound, full screen; a tap on
 * the video or Space plays and pauses), not the browser's.
 * A chunk hands over to the next when it ends, and also when it is stuck at its end (a streamed transcode can come out
 * shorter than its duration says, and then 'ended' never fires); a next chunk that doesn't come is asked for again
 * once, then the viewer can tap to retry (eventPlayback.ts chunkStep).
 * SD (the server's transcode) by default on a phone when the server can, else HD, with a switch; SD refused for lack of
 * transcode slots plays HD. Without a recording of the event: its own clip.mp4 if it still has one, else the snapshot.
 * The pure decisions are in eventPlayback.ts. `startAt`: where to start instead (a phone coming back to the Clip tab, or
 * a chat frame tapped while the player was hidden), read when it mounts.
 */
export function EventPlayer({ e, site, startAt, onTime, handle, onFootage }: {
  e: NvrEvent; site: SiteApi;
  startAt?: { t: number; paused: boolean } | null;
  /** the playhead (epoch seconds), a few times a second while it moves */
  onTime?: (t: number) => void;
  handle?: React.RefObject<EventPlayerHandle | null>;
  /** is any footage of the event there (the clip, or a recording overlapping it)? undefined while that isn't known */
  onFootage?: (available: boolean | undefined) => void;
}) {
  const cam = e.camera_id;
  const isPhone = useIsPhone();
  const hasClip = Boolean(e.clip);
  const live = e.status === "open" || e.status === "pending";
  const t0 = clipOrigin(e);
  const from = startAt?.t ?? playStart(e);
  const [zoom, setZoom] = useState(0);
  const [win, setWin] = useState<Win>(() => {
    const now = nowS();
    return followWindow(stripWindow(e.start_ts, eventEnd(e, now), now), from);
  });
  const [listing, setListing] = useState<Listing>({ spans: undefined, events: [], from: 0, to: 0 });
  const spans = listing.spans;
  const [caps, setCaps] = useState<boolean | undefined>(undefined);
  const [picked, setPicked] = useState<PlaybackQuality | null>(null);   // the SD/HD switch
  const [sdBusy, setSdBusy] = useState(false);                           // SD was refused (503): HD for this viewer
  const quality = sdBusy ? "hd" : picked ?? defaultQuality(isPhone, caps);
  const remote = useMemo(() => viaHub(site.playbackUrl(cam, 0, 1)), [site, cam]);

  const [source, setSource] = useState<Source | null>(null);
  const [chunk, setChunk] = useState<Chunk | null>(null);
  const [t, setT] = useState(from);
  const [scrubT, setScrubT] = useState<number | null>(null);
  const [stopped, setStopped] = useState<Stopped>(null);
  const video = useRef<HTMLVideoElement | null>(null);
  const box = useRef<HTMLDivElement | null>(null);
  const tRef = useRef(from);
  // The viewer's play / pause, kept across chunks. Only the viewer changes it (the controls, a tap, Space, a paused
  // seek) or a pause that isn't the chunk running out: Chrome on Android pauses before 'ended' with `ended` still false.
  const wantPlay = useRef(!startAt?.paused);
  const [want, setWantState] = useState(wantPlay.current);
  const setWant = (b: boolean) => { wantPlay.current = b; setWantState(b); };
  const [muted, setMuted] = useState(true);   // muted autoplay is allowed everywhere; sound is the viewer's choice
  const [full, setFull] = useState(false);
  const played = useRef(false);                // some recording chunk played: a later failure stops there, no fallback
  const prefetch = useRef<Prefetched | null>(null);
  const blobInUse = useRef<string | null>(null);
  const firstChunk = useRef(true);             // the snapshot is the poster of the first chunk only, not of every switch
  const advancing = useRef<number | null>(null);   // the chunk (key) already handing over to the next
  const st = useRef({ spans, quality, remote, chunk, source, win, stopped });
  st.current = { spans, quality, remote, chunk, source, win, stopped };

  useEffect(() => { canSd(site).then(setCaps); }, [site]);

  // The recordings listing (and the camera's events) for the window, with a margin either side so previous / next can
  // reach past it (eventPlayback.ts listingRange); listed again when the window, or zooming out, leaves that margin.
  const list = useCallback((w: Win) => {
    const { from: lo, to: hi } = listingRange(w, nowS());
    return site.recordings(cam, lo, hi)
      .then((r) => ({ spans: mergeSpans(r.spans), events: r.events, from: lo, to: hi }))
      .catch(() => ({ spans: null, events: [], from: lo, to: hi }));
  }, [site, cam]);
  useEffect(() => {
    if (listing.spans !== undefined && !needsListing(listing, win, nowS())) return;
    let alive = true;
    list(win).then((l) => { if (alive) setListing(l); });
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [win.start, win.end, list]);

  const available = footageAvailable({ spans, start: e.start_ts, end: eventEnd(e, nowS()), hasClip });
  useEffect(() => { onFootage?.(available); }, [available, onFootage]);

  const moveTo = (x: number) => {
    tRef.current = x;
    setT(x);
    onTime?.(x);
    setWin((w) => followWindow(w, x));
  };

  /** play(), and if the browser refuses sound without a tap, muted; refused even muted: show it paused. */
  const playNow = (v: HTMLVideoElement) => {
    v.play().catch((err: unknown) => {
      if (v !== video.current || (err as Error)?.name !== "NotAllowedError") return;   // AbortError: a new load took over
      if (!v.muted) {
        v.muted = true;
        setMuted(true);
        v.play().catch(() => { if (v === video.current) setWant(false); });
      } else setWant(false);
    });
  };
  // The <video> on screen; when React drops one (the next chunk, a seek, the clip) it is released at once.
  const attachVideo = useCallback((el: HTMLVideoElement | null) => {
    if (!el) return;
    video.current = el;
    return () => {
      release(el);
      if (video.current === el) video.current = null;
    };
  }, []);

  /** Start a recording chunk at `at`: the prefetched one when it is that, else a request (`cont` = a continuation;
   *  `retry` = asked for again: always a request). */
  const load = (at: number, cont = false, retry = 0) => {
    const { spans, quality, remote } = st.current;
    if (quality == null) return;
    if (blobInUse.current) URL.revokeObjectURL(blobInUse.current);
    blobInUse.current = null;
    let { len } = chunkAt(at, spans ?? null, { remote, cont, now: nowS() });
    let src = site.playbackUrl(cam, at, len, quality);
    const p = prefetch.current;
    if (retry === 0 && p && Math.abs(p.start - at) < 1 && p.url) {
      len = p.len;
      src = p.url;
      blobInUse.current = p.url;
      prefetch.current = null;           // handed over; not revoked
    } else {
      dropPrefetch(p);                   // not there yet: the next chunk starts now anyway, from the network
      prefetch.current = null;
    }
    setStopped(null);
    setSource({ kind: "recording", at });
    setChunk({ start: at, len, src, key: ++chunkSeq, cont, retry, asked: performance.now() });
    moveTo(at);
  };
  /** The chunk is running out: download the next one now so the switch is seamless. */
  const prefetchNext = (c: Chunk) => {
    if (prefetch.current) return;
    const { spans, quality, remote } = st.current;
    const next = nextChunkStart(c.start, c.len, spans ?? null, nowS());
    if (next == null || quality == null) return;
    const { len } = chunkAt(next, spans ?? null, { remote, cont: true, now: nowS() });
    prefetch.current = prefetchChunk(site.playbackUrl(cam, next, len, quality), next, len);
  };
  /** Ask for chunk `c` again, from where it got to (once; `failed` after that: the viewer taps to retry). */
  const again = (c: Chunk, force = false) => {
    if (st.current.chunk?.key !== c.key) return;
    const at = tRef.current >= c.start && tRef.current < c.start + c.len ? tRef.current : c.start;
    if (force || c.retry < 1) load(at, c.cont, force ? 0 : c.retry + 1);
    else setStopped("failed");
  };
  /** Chunk `c` ended, or is stuck at its end: on to the next one at once (from the network if the prefetch isn't
   *  there yet), or the end of the recording / live. */
  const advance = async (c: Chunk, v: HTMLVideoElement) => {
    if (advancing.current === c.key || st.current.chunk?.key !== c.key) return;
    advancing.current = c.key;
    const d = v.duration;
    const playedS = v.ended ? (Number.isFinite(d) && d > 0 ? d : c.len) : v.currentTime > 0.5 ? v.currentTime : c.len;
    let at = nextChunkStart(c.start, playedS, st.current.spans ?? null, nowS());
    if (at == null && c.start + playedS < nowS() - 5) {
      // the listing may be older than this footage (an event still being tracked): list again before calling it the end
      const l = await list(st.current.win);
      setListing(l);
      at = nextChunkStart(c.start, playedS, l.spans, nowS());
    }
    if (st.current.chunk?.key !== c.key) return;
    if (at != null) load(at, true);
    else setStopped(c.start + playedS >= nowS() - 10 ? "live" : "end");
  };

  // The first thing to play, once the listing and the quality are known.
  useEffect(() => {
    if (source || quality == null) return;
    const first = initialSource({ spans, from, until: eventEnd(e, nowS()) + 5, hasClip });
    if (!first) return;
    if (first.kind === "recording") load(first.at);
    else setSource(first);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [spans, quality, source]);
  // The SD/HD switch (or SD refused): the same place in the other quality; anything prefetched was the old one.
  const lastQuality = useRef(quality);
  useEffect(() => {
    if (lastQuality.current === quality) return;
    lastQuality.current = quality;
    dropPrefetch(prefetch.current);
    prefetch.current = null;
    if (st.current.source?.kind === "recording" && st.current.chunk) load(tRef.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [quality]);
  useEffect(() => () => {
    dropPrefetch(prefetch.current);
    if (blobInUse.current) URL.revokeObjectURL(blobInUse.current);
  }, []);

  // The chunk's watchdog: twice a second, is it playing as it should? Stuck at its end: on to the next; paused but
  // wanted playing: play again; stuck mid-way or never started: ask again (eventPlayback.ts chunkStep).
  useEffect(() => {
    if (source?.kind !== "recording" || !chunk) return;
    const c = chunk;
    let lastCt = NaN, lastMove = performance.now();
    const id = window.setInterval(() => {
      const v = video.current;
      if (!v || st.current.chunk?.key !== c.key || st.current.stopped) return;
      const now = performance.now();
      if (v.currentTime !== lastCt || v.seeking || !wantPlay.current) { lastCt = v.currentTime; lastMove = now; }
      const step = chunkStep({
        ct: v.currentTime, len: c.len, duration: v.duration, bufferedEnd: bufferedEnd(v, v.currentTime),
        idleS: (now - lastMove) / 1000, sinceLoadS: (now - c.asked) / 1000, started: v.readyState >= HTMLMediaElement.HAVE_METADATA,
        wantPlay: wantPlay.current, paused: v.paused, ended: v.ended, loading: v.networkState === HTMLMediaElement.NETWORK_LOADING,
        remote: st.current.remote,
      });
      if (step === "advance") advance(c, v);
      else if (step === "play") { lastMove = now; playNow(v); }
      else if (step === "reload") { lastMove = now; again(c, true); }
      else if (step === "retry") again(c);
    }, 500);
    return () => window.clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chunk?.key, source?.kind]);

  /** Go to `to`: within the chunk when it is there already, else a new chunk; a gap goes to the next recording (`dir`
   *  -1: the end of the one before). */
  const seek = (to: number, pause = false, dir: -1 | 1 = 1) => {
    if (pause) { setWant(false); video.current?.pause(); }
    const { spans, source, chunk } = st.current;
    const v = video.current;
    const inClip = (x: number) => source?.kind === "clip" && v && x >= t0 && x <= t0 + (Number.isFinite(v.duration) ? v.duration : 0);
    const recorded = spans === null || (spans && spanAt(spans, to));
    if (!recorded && inClip(to)) { v!.currentTime = to - t0; return moveTo(to); }
    const at = recorded ? to : spans ? snapToRecorded(spans, to, dir) : null;
    if (at == null) {
      if (source?.kind === "clip" && v) { v.currentTime = Math.max(0, to - t0); moveTo(t0 + v.currentTime); }
      return;
    }
    if (source?.kind === "recording" && chunk && v && at >= chunk.start && at < chunk.start + chunk.len && isBuffered(v, at - chunk.start)) {
      v.currentTime = at - chunk.start;
      setStopped(null);
      if (wantPlay.current && v.paused) playNow(v);
      return moveTo(at);
    }
    load(at);
  };
  useEffect(() => { if (handle) handle.current = { seek }; });
  useEffect(() => () => { if (handle) handle.current = null; }, [handle]);   // hidden (a phone's other tab): nothing to seek

  // ---- the video's own controls
  const toggle = () => {
    const v = video.current;
    const c = st.current.chunk;
    if (st.current.stopped === "failed" && c) { setWant(true); return again(c, true); }
    if (wantPlay.current) { setWant(false); v?.pause(); return; }
    setWant(true);
    if (st.current.stopped === "end" || st.current.stopped === "live") return seek(playStart(e));   // from the top
    if (!v) return;
    if (st.current.source?.kind === "recording" && c && v.ended) advance(c, v);   // play() would restart the chunk
    else playNow(v);
  };
  const toggleMute = () => {
    const m = !muted;
    setMuted(m);
    if (video.current) video.current.muted = m;
  };
  const toggleFull = () => {
    if (fullscreenEl()) {
      const d = document as FsDoc;
      if (d.exitFullscreen) d.exitFullscreen().catch(() => {});
      else d.webkitExitFullscreen?.();
      return;
    }
    const el = box.current as FsEl | null;
    if (el?.requestFullscreen) el.requestFullscreen().catch(() => {});
    else if (el?.webkitRequestFullscreen) el.webkitRequestFullscreen();
    else (video.current as (HTMLVideoElement & { webkitEnterFullscreen?: () => void }) | null)?.webkitEnterFullscreen?.();   // iPhone: the video alone
  };
  useEffect(() => {
    const on = () => setFull(!!box.current && fullscreenEl() === box.current);
    document.addEventListener("fullscreenchange", on);
    document.addEventListener("webkitfullscreenchange", on);
    return () => { document.removeEventListener("fullscreenchange", on); document.removeEventListener("webkitfullscreenchange", on); };
  }, []);
  const onPlayerKey = (ev: React.KeyboardEvent) => {
    if (ev.key !== " " || ev.altKey || ev.ctrlKey || ev.metaKey) return;
    const el = ev.target as HTMLElement;
    if (["BUTTON", "INPUT", "TEXTAREA", "SELECT", "A"].includes(el.tagName) || el.isContentEditable) return;   // a button's own Space
    ev.preventDefault();
    toggle();
  };

  // Scrub previews: frames from the recording at the dragged time; refined to the exact frame when the finger rests.
  const frameUrlFor = useCallback((c: string, at: number, w?: number, exact?: boolean) => site.frameUrl(c, at, w, exact), [site]);
  const frames = useLatestFrame(640, frameUrlFor);
  useEffect(() => {
    if (scrubT == null) return;
    frames.request(cam, scrubT);
    const id = window.setTimeout(() => frames.request(cam, scrubT, true), 200);
    return () => window.clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scrubT, cam]);
  useEffect(() => {
    if (scrubT != null || !frames.shot) return;
    frames.stop();
    const id = window.setTimeout(frames.clear, 4000);   // the video catching up (onPlaying / onSeeked) clears it sooner
    return () => window.clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scrubT]);
  const caughtUp = () => { if (scrubT == null && frames.shot) frames.clear(); };

  // ---- the strip: drag or tap to scrub; a tap on a tick is the tick's own click (it goes there)
  const strip = useRef<HTMLDivElement | null>(null);
  const [stripW, setStripW] = useState(0);
  useEffect(() => {
    const el = strip.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => setStripW(el.clientWidth));
    ro.observe(el);
    setStripW(el.clientWidth);
    return () => ro.disconnect();
  }, []);
  const drag = useRef<{ x0: number; id: number; moving: boolean } | null>(null);
  const timeAtX = (clientX: number) => {
    const r = strip.current!.getBoundingClientRect();
    return atFrac((clientX - r.left) / Math.max(1, r.width), st.current.win);
  };
  const onPointerDown = (ev: React.PointerEvent) => {
    if (ev.button !== 0) return;
    drag.current = { x0: ev.clientX, id: ev.pointerId, moving: false };
  };
  const onPointerMove = (ev: React.PointerEvent) => {
    const d = drag.current;
    if (!d || d.id !== ev.pointerId) return;
    if (!d.moving) {
      if (Math.abs(ev.clientX - d.x0) < 6) return;
      d.moving = true;
      strip.current?.setPointerCapture(ev.pointerId);   // only now: a plain tap must still reach a tick's button
    }
    setScrubT(timeAtX(ev.clientX));
  };
  const onPointerUp = (ev: React.PointerEvent) => {
    const d = drag.current;
    drag.current = null;
    if (!d || d.id !== ev.pointerId) return;
    if (d.moving) { setScrubT(null); seek(timeAtX(ev.clientX)); return; }
    if (!(ev.target as Element).closest(".ep-tick")) seek(timeAtX(ev.clientX));
  };
  const onPointerCancel = () => { drag.current = null; setScrubT(null); };
  const onKeyDown = (ev: React.KeyboardEvent) => {
    if (ev.key !== "ArrowLeft" && ev.key !== "ArrowRight") return;
    ev.preventDefault();
    const back = ev.key === "ArrowLeft";
    seek(tRef.current + (back ? -KEY_STEP_S : KEY_STEP_S), false, back ? -1 : 1);
  };

  const now = nowS();
  const end = eventEnd(e, now);
  const zoomTo = (level: number) => {
    setZoom(level);
    setWin(zoomWindow({ start: e.start_ts, end: eventEnd(e, nowS()) }, level, scrubT ?? tRef.current, nowS()));
  };
  const label = (x: TimelineEvent) => `${x.yolo_class ?? x.camera_class} · ${fmtTime(x.start_ts)}${x.status === "rejected" ? " · rejected" : ""}`;
  const marks = ticks(listing.events, win, e.id, label);
  const all = listing.events.some((x) => x.id === e.id) ? listing.events : [...listing.events, { ...e, verdict: null, has_synopsis: 0 } as TimelineEvent];
  const prev = neighbour(all, t, -1), next = neighbour(all, t, 1);
  const pieces = spans ? coverage(spans, win) : null;
  const gaps = pieces?.some((p) => !p.recorded && p.end - p.start > 2);
  // round times between the end labels: about one per 80 px, none within 95 px of an end (an end label is ~60 px wide)
  const sw = stripW || (isPhone ? 340 : 640);
  const scale = scaleMarks(win, Math.max(0, Math.floor(sw / 80) - 2), Math.min(0.4, 95 / sw));
  const shownT = scrubT ?? t;
  const pct = (x: number) => `${(toFrac(x, win) * 100).toFixed(3)}%`;
  const poster = e.snapshot ? site.media(e, "snapshot.jpg", 960) : undefined;
  const hasVideo = (source?.kind === "recording" && chunk) || source?.kind === "clip";

  const onError = async (c: Chunk) => {
    const status = c.src.startsWith("blob:") ? null : await httpStatus(c.src);
    if (st.current.chunk?.key !== c.key) return;   // already replaced
    const step = afterPlaybackError({ status, quality: st.current.quality ?? "hd", played: played.current, hasClip, retried: c.retry > 0, cont: c.cont });
    if (step === "retry") window.setTimeout(() => again(c), status === 503 ? 1000 : 300);
    else if (step === "hd") setSdBusy(true);            // quietly: the quality effect reloads this place in HD
    else if (step === "stop") setStopped("failed");
    else { setChunk(null); setSource({ kind: step }); }
  };

  return (
    <div className="ep" onKeyDown={onPlayerKey}>
      <div className={`ep-video${full ? " full" : ""}`} ref={box} tabIndex={hasVideo ? 0 : undefined}
        role={hasVideo ? "group" : undefined} aria-label={hasVideo ? "Event video (Space: play or pause)" : undefined}>
        {source?.kind === "recording" && chunk ? (
          <video key={chunk.key} ref={attachVideo} className="clip" src={chunk.src} poster={firstChunk.current ? poster : undefined}
            muted={muted} playsInline autoPlay={wantPlay.current} preload="auto"
            onClick={toggle}
            onLoadedMetadata={(ev) => { if (wantPlay.current) playNow(ev.currentTarget); }}
            onCanPlay={(ev) => { if (wantPlay.current && ev.currentTarget.paused && !ev.currentTarget.ended) playNow(ev.currentTarget); }}
            onPlaying={() => {
              played.current = true;
              firstChunk.current = false;
              caughtUp();
              if (st.current.stopped === "failed") setStopped(null);   // it came after all
            }}
            onSeeked={caughtUp}
            onPlay={(ev) => { if (ev.currentTarget === video.current) setWant(true); }}
            onPause={(ev) => {
              const v = ev.currentTarget;
              if (v !== video.current || pauseAtEnd({ ct: v.currentTime, len: chunk.len, duration: v.duration, ended: v.ended })) return;
              setWant(false);
            }}
            onTimeUpdate={(ev) => {
              const at = chunk.start + ev.currentTarget.currentTime;
              if (scrubT == null) moveTo(at);
              if (chunk.start + chunk.len - at < prefetchLead(chunk.len, PREFETCH_LEAD_S)) prefetchNext(chunk);
            }}
            onError={() => onError(chunk)}
            onEnded={(ev) => advance(chunk, ev.currentTarget)} />
        ) : source?.kind === "clip" ? (
          <video ref={attachVideo} className="clip" src={site.media(e, "clip.mp4")} poster={poster} autoPlay={wantPlay.current} muted={muted} playsInline
            onClick={toggle}
            onTimeUpdate={(ev) => { if (scrubT == null) moveTo(t0 + ev.currentTarget.currentTime); }}
            onPlay={(ev) => { if (ev.currentTarget === video.current) setWant(true); }}
            onPause={(ev) => { if (ev.currentTarget === video.current && !ev.currentTarget.ended) setWant(false); }}
            onEnded={() => setWant(false)}
            onSeeked={caughtUp} onPlaying={caughtUp}
            onError={() => setSource({ kind: "still" })} />
        ) : e.snapshot ? (
          <img className="clip" src={site.media(e, "snapshot.jpg", 960)} alt="" />
        ) : (
          <div className="clip thumb-empty">{source?.kind === "still" && !live ? "The footage is no longer recorded" : source ? "No media yet" : "Loading…"}</div>
        )}
        {frames.shot && (
          <div className="ep-preview">
            {frames.shot.url ? <img src={frames.shot.url} alt="" /> : <div className="ep-msg">No recording here</div>}
          </div>
        )}
        {/* one play control at a time: the big one while paused, the bar's Pause while playing */}
        {hasVideo && !want && !stopped && !frames.shot && (
          <button type="button" className="ep-big" onClick={toggle} aria-label="Play" title="Play (Space)"><Icon name="play" size={30} /></button>
        )}
        {stopped === "failed" && chunk ? (
          <button type="button" className="ep-retry" onClick={() => { setWant(true); again(chunk, true); }}>
            {played.current || chunk.cont ? "Couldn't load the next part" : "Couldn't load the recording"} — tap to retry
          </button>
        ) : stopped && !frames.shot ? (
          <div className="ep-msg ep-msg-bar">{stopped === "live" ? "Caught up with live" : "End of the recording"}</div>
        ) : null}
        {hasVideo && (
          <div className="ep-bar">
            {(want || stopped) && (
              <button type="button" onClick={toggle} aria-label={want ? "Pause" : "Play"} title={want ? "Pause (Space)" : "Play (Space)"}>
                <Icon name={want ? "pause" : "play"} size={20} />
              </button>
            )}
            <span className="grow" />
            <button type="button" onClick={toggleMute} aria-label={muted ? "Turn sound on" : "Mute"} aria-pressed={!muted} title={muted ? "Sound on" : "Mute"}>
              <Icon name={muted ? "volumeOff" : "volume"} size={20} />
            </button>
            <button type="button" onClick={toggleFull} aria-label={full ? "Exit full screen" : "Full screen"} title={full ? "Exit full screen" : "Full screen"}>
              <Icon name={full ? "shrink" : "expand"} size={20} />
            </button>
          </div>
        )}
      </div>
      {source?.kind === "clip" && <p className="muted small">The event's own clip: the recording around it is no longer kept.</p>}
      {source?.kind === "still" && e.snapshot && !live && (
        <p className="muted small">The footage for this event is no longer recorded (retention policy); only the snapshot remains.</p>
      )}

      <div className="ep-strip" ref={strip} onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={onPointerUp}
        onPointerCancel={onPointerCancel}>
        <div className={`ep-track ${pieces ? "" : "unknown"}`} role="slider" tabIndex={0} onKeyDown={onKeyDown}
          aria-label="Playback position around the event (arrow keys: 5 s; Space: play or pause)"
          aria-valuemin={0} aria-valuemax={Math.round(win.end - win.start)} aria-valuenow={Math.round(Math.min(win.end, Math.max(win.start, shownT)) - win.start)}
          aria-valuetext={`${clock(shownT)} (${relLabel(shownT, e.start_ts, end)})`}>
          {pieces?.map((p) => (
            <div key={p.start} className={p.recorded ? "ep-rec" : "ep-gap"} style={{ left: pct(p.start), width: pct(win.start + p.end - p.start) }}
              title={p.recorded ? `Recorded ${clock(p.start)}–${clock(p.end)}` : `Not recorded ${clock(p.start)}–${clock(p.end)}`} />
          ))}
          {scale.map((m) => <div key={m.t} className="ep-grid" style={{ left: `${(m.frac * 100).toFixed(3)}%` }} />)}
          <div className="ep-event" style={{ left: pct(e.start_ts), width: `max(4px, ${((toFrac(end, win) - toFrac(e.start_ts, win)) * 100).toFixed(3)}%)` }}
            title={`This event: ${clock(e.start_ts)}–${clock(end)}`} />
          <div className="ep-playhead" style={{ left: pct(shownT) }} />
        </div>
        {marks.map((m) => (
          <button key={m.id} type="button" tabIndex={-1} className={`ep-tick ${m.cls}`} style={{ left: `${(m.frac * 100).toFixed(3)}%` }}
            title={m.title} aria-label={`Go to ${m.title}`}
            onClick={() => { const x = listing.events.find((ev) => ev.id === m.id); if (x) seek(jumpTarget(x)); }} />
        ))}
      </div>
      <div className="ep-scale small muted" aria-hidden="true">
        <span className="ep-scale-end">{clock(win.start)}</span>
        {scale.map((m) => <span key={m.t} className="ep-scale-mark" style={{ left: `${(m.frac * 100).toFixed(3)}%` }}>{m.label}</span>)}
        <span className="ep-scale-end">{clock(win.end)}</span>
      </div>
      {gaps ? <div className="ep-legend small muted"><i className="ep-key rec" /> recorded <i className="ep-key gap" /> not recorded</div> : null}
      <div className="ep-controls">
        <button type="button" className="ghost" disabled={!prev} onClick={() => prev && seek(jumpTarget(prev))}
          aria-label="Previous event on this camera" title={prev ? `Previous event: ${label(prev)}` : "No earlier event on this camera"}>◀ Prev</button>
        <button type="button" className="ghost" title="Back to the start of this event"
          onClick={() => { const to = jumpTarget(e); seek(to); setWin(zoomWindow({ start: e.start_ts, end: eventEnd(e, nowS()) }, zoom, to, nowS())); }}>Event</button>
        <button type="button" className="ghost" disabled={!next} onClick={() => next && seek(jumpTarget(next))}
          aria-label="Next event on this camera" title={next ? `Next event: ${label(next)}` : "No later event on this camera"}>Next ▶</button>
        <span className="ep-time"><strong>{clock(shownT)}</strong> <span className="muted">{relLabel(shownT, e.start_ts, end)}</span></span>
        <div className="ep-zoom" role="group" aria-label={`Strip zoom: ${zoomLabel(zoom)} around the event`}>
          <button type="button" className="ghost" disabled={zoom >= ZOOM_HALF_S.length - 1} onClick={() => zoomTo(zoom + 1)}
            aria-label="Zoom the strip out" title="Zoom out: more time on the strip">−</button>
          <span className="small muted">{zoomLabel(zoom)}</span>
          <button type="button" className="ghost" disabled={zoom <= 0} onClick={() => zoomTo(zoom - 1)}
            aria-label="Zoom the strip in" title="Zoom in: less time on the strip">+</button>
        </div>
        {caps && source?.kind === "recording" && (
          <div className="segmented ep-quality" role="group" aria-label="Playback quality">
            <button type="button" className={quality === "sd" ? "active" : ""} aria-pressed={quality === "sd"} disabled={sdBusy}
              title={sdBusy ? "The server is busy with other SD streams" : "Low bitrate (720p): quicker on a slow link"} onClick={() => setPicked("sd")}>SD</button>
            <button type="button" className={quality === "hd" ? "active" : ""} aria-pressed={quality === "hd"}
              title="The recording as stored" onClick={() => setPicked("hd")}>HD</button>
          </div>
        )}
      </div>
    </div>
  );
}
