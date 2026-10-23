import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { fmtTime, type NvrEvent, type PlaybackQuality, type SiteApi, type TimelineEvent } from "./api";
import {
  FarCache, KEY_STEP_S, STEP_STATUSES, afterPlaybackError, anyRecorded, asTimelineEvent, atFrac, centreOn, chunkAt, chunkStep,
  clampStrip, clipOrigin, clock, coverage, defaultQuality, eventEnd, farNeighbour, followWindow, footageAvailable, halfLabel,
  initialSource, jumpTarget, listedEvents, listingRange, needsListing, nextChunkStart, pauseAtEnd, playStart, prefetchLead,
  relLabel, scaleMarks, sdUsable, snapToRecorded, stepButton, stepPlan, stripLimits, stripWindow, ticks, toFrac, viaHub,
  withSelected, zoomStep, zoomWindowWidth, type Source, type StepButton, type StepPlan, type Win,
} from "./eventPlayback";
import { httpStatus } from "./liveFallback";
import { PREFETCH_LEAD_S, dropPrefetch, isBuffered, mergeSpans, nowS, prefetchChunk, spanAt, useLatestFrame, type Prefetched, type Span } from "./playback";
import { fracIn, hoverFrameTime, isDrag, panWindow, pinchStart, pinchWindow, thumbLeft, wheelWindow, type PinchStart } from "./timeZoom";
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

/** Previous / next beyond the listing (eventPlayback.ts farNeighbour): the servers' answers, per server client. */
const farCaches = new WeakMap<SiteApi, FarCache<TimelineEvent>>();
const farCacheFor = (site: SiteApi): FarCache<TimelineEvent> => {
  let c = farCaches.get(site);
  if (!c) {
    c = new FarCache<TimelineEvent>();
    farCaches.set(site, c);
  }
  return c;
};

type Listing = { spans: Span[] | null | undefined; events: TimelineEvent[]; from: number; to: number };
/** A recording chunk on screen: `cont` = a continuation (full length), `retry` = how many times it was asked for again,
 *  `asked` = when (performance.now()). */
type Chunk = { start: number; len: number; src: string; key: number; cont: boolean; retry: number; asked: number };
type Stopped = "failed" | "end" | "live" | null;
let chunkSeq = 0;
/** The event the strip is about: the viewer's event, or one picked on the strip (a tick, previous / next) whose full
 *  record the viewer is still fetching. */
type Selected = Pick<TimelineEvent, "id" | "start_ts" | "end_ts" | "status" | "camera_class" | "yolo_class">;
/** A press on the strip: `pan` drags the window, `scrub` the playhead (grabbed within GRAB_PX of it). */
type StripDrag = { mode: "pan" | "scrub"; id: number; x0: number; w0: Win; moving: boolean; type: string };
/** How close to the playhead (px) a press grabs it to scrub, as on the Timeline page; a fingertip is less precise. */
const GRAB_PX = 12;
const GRAB_TOUCH_PX = 28;
/** Hover thumbnails: the Timeline page's width, so the two share the browser's cache of the same frame URLs. */
const THUMB_W = 320;

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
export function EventPlayer({ e, site, startAt, onTime, handle, onFootage, onSelectEvent }: {
  e: NvrEvent; site: SiteApi;
  startAt?: { t: number; paused: boolean } | null;
  /** the playhead (epoch seconds), a few times a second while it moves */
  onTime?: (t: number) => void;
  handle?: React.RefObject<EventPlayerHandle | null>;
  /** is any footage of the event there (the clip, or a recording overlapping it)? undefined while that isn't known */
  onFootage?: (available: boolean | undefined) => void;
  /** the viewer switched to another event of the camera on the strip (a tick, previous / next): the video goes on from
   *  there; `remount` = it has no recording, so the viewer should start a new player on it (its clip or snapshot) */
  onSelectEvent?: (x: TimelineEvent, o: { remount: boolean }) => void;
}) {
  const cam = e.camera_id;
  const isPhone = useIsPhone();
  const hasClip = Boolean(e.clip);
  const live = e.status === "open" || e.status === "pending";
  const t0 = clipOrigin(e);
  const from = startAt?.t ?? playStart(e);
  // the event the strip is about: `e`, or one picked on the strip until the viewer hands over its full record as `e`
  const [pick, setPick] = useState<Selected | null>(null);
  const cur: Selected = pick && pick.id !== e.id ? pick : e;
  useEffect(() => { setPick(null); }, [e.id]);   // the viewer's record of it arrived (or it moved on): `e` again
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
  const lim = stripLimits({ start: cur.start_ts, end: eventEnd(cur, nowS()) });   // wheel / pinch zoom limits
  // a jump to an event beyond the listing (previous / next far away), waiting for the listing around it (select)
  const [jumping, setJumping] = useState<{ id: number; at: number } | null>(null);
  const st = useRef({ spans, quality, remote, chunk, source, win, stopped, lim, listing, jumping });
  st.current = { spans, quality, remote, chunk, source, win, stopped, lim, listing, jumping };

  useEffect(() => { canSd(site).then(setCaps); }, [site]);

  // The recordings listing (and the camera's events) for the window, with a margin either side so previous / next can
  // reach past it (eventPlayback.ts listingRange); listed again when the window, or zooming out, leaves that margin.
  const list = useCallback((w: Win) => {
    const { from: lo, to: hi } = listingRange(w, nowS());
    return site.recordings(cam, lo, hi)
      // masked events (a mask, or hidden by hand) are no ticks and no Prev / Next stops
      .then((r) => ({ spans: mergeSpans(r.spans), events: listedEvents(r.events), from: lo, to: hi }))
      .catch(() => ({ spans: null, events: [], from: lo, to: hi }));
  }, [site, cam]);
  // (at once the first time; then after the strip has rested 200 ms, so a drag or wheel doesn't list at every step)
  useEffect(() => {
    if (listing.spans !== undefined && !needsListing(listing, win, nowS())) return;
    let alive = true;
    const go = () => { list(win).then((l) => { if (alive) setListing(l); }); };
    const id = listing.spans === undefined ? (go(), 0) : window.setTimeout(go, 200);
    return () => { alive = false; window.clearTimeout(id); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [win.start, win.end, list]);

  const available = footageAvailable({ spans, start: e.start_ts, end: eventEnd(e, nowS()), hasClip });
  useEffect(() => { onFootage?.(available); }, [available, onFootage]);

  /** The playhead is at `x` now. `jump` (a seek, a new chunk): the strip follows it there; playing on, it follows only
   *  while the playhead was in view (the viewer may have panned or zoomed away to look elsewhere). */
  const moveTo = (x: number, jump = true) => {
    const prev = tRef.current;
    tRef.current = x;
    setT(x);
    onTime?.(x);
    setWin((w) => followWindow(w, x, jump ? undefined : prev));
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
    moveTo(at, !cont);
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
    if (st.current.jumping) setJumping(null);   // somewhere else now
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
    if (st.current.stopped === "end" || st.current.stopped === "live") return seek(playStart(cur));   // from the top
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

  // ---- the strip, like the Timeline page's (timeZoom.ts): drag to pan, drag the playhead to scrub, a click or tap to
  // go there (a tick: switch to that event), the wheel or a pinch to zoom around the cursor, hover for a thumbnail
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
  const drag = useRef<StripDrag | null>(null);
  const [dragMode, setDragMode] = useState<StripDrag["mode"] | null>(null);   // a drag under way (cursor, no hover)
  const touches = useRef(new Map<number, number>());   // touch pointerId -> clientX, for the pinch
  const pinch = useRef<PinchStart | null>(null);
  const timeAtX = (clientX: number) => atFrac(fracIn(clientX, strip.current!.getBoundingClientRect()), st.current.win);

  // Hover: the recording's frame at that time (the Timeline page's useLatestFrame and frame URLs: one request in flight,
  // whole seconds), or over a tick that event's snapshot.
  const [hover, setHover] = useState<{ x: number; t: number; tick: number | null } | null>(null);
  const hoverFrames = useLatestFrame(THUMB_W, frameUrlFor);
  const endHover = () => { setHover(null); hoverFrames.clear(); };
  const endHoverRef = useRef(endHover);
  endHoverRef.current = endHover;
  const hoverAt = (clientX: number, target: Element) => {
    const r = strip.current!.getBoundingClientRect();
    const t = atFrac(fracIn(clientX, r), st.current.win);
    const tickEl = target.closest<HTMLElement>(".ep-tick");
    const tick = tickEl ? Number(tickEl.dataset.id) : null;
    setHover({ x: clientX - r.left, t, tick });
    const at = hoverFrameTime(t, nowS());
    const sp = st.current.spans;
    if (tick != null || at == null || (sp && !spanAt(sp, t))) hoverFrames.stop();   // a snapshot, live, or not recorded
    else hoverFrames.request(cam, at);
  };

  const onPointerDown = (ev: React.PointerEvent) => {
    if (ev.button !== 0) return;
    const touch = ev.pointerType === "touch";
    if (touch) {
      touches.current.set(ev.pointerId, ev.clientX);
      if (touches.current.size === 2) {
        // a second finger: from pan / scrub to pinch zoom
        if (drag.current?.mode === "scrub" && drag.current.moving) setScrubT(null);
        drag.current = null;
        setDragMode(null);
        const xs = [...touches.current.values()];
        pinch.current = pinchStart(xs[0], xs[1], st.current.win);
        try { strip.current?.setPointerCapture(ev.pointerId); } catch { /* released */ }
        return;
      }
    }
    const r = strip.current!.getBoundingClientRect();
    const phX = toFrac(shownT, win) * r.width;
    const grab = shownT >= win.start && shownT <= win.end && Math.abs(ev.clientX - r.left - phX) <= (touch ? GRAB_TOUCH_PX : GRAB_PX);
    drag.current = { mode: grab ? "scrub" : "pan", id: ev.pointerId, x0: ev.clientX, w0: st.current.win, moving: false, type: ev.pointerType };
    endHover();
  };
  const onPointerMove = (ev: React.PointerEvent) => {
    if (touches.current.has(ev.pointerId)) {
      touches.current.set(ev.pointerId, ev.clientX);
      const xs = [...touches.current.values()];
      if (pinch.current) {
        if (xs.length >= 2) {
          const w = pinchWindow(pinch.current, xs[0], xs[1], strip.current!.getBoundingClientRect(), st.current.lim);
          setWin(clampStrip(w, nowS(), st.current.lim));
        }
        return;
      }
    }
    const d = drag.current;
    if (!d) { if (ev.pointerType !== "touch") hoverAt(ev.clientX, ev.target as Element); return; }
    if (d.id !== ev.pointerId) return;
    if (!d.moving) {
      if (!isDrag(ev.clientX - d.x0, d.type)) return;
      d.moving = true;
      setDragMode(d.mode);
      strip.current?.setPointerCapture(ev.pointerId);   // only now: a plain tap must still reach a tick's button
    }
    if (d.mode === "scrub") setScrubT(timeAtX(ev.clientX));
    else setWin(clampStrip(panWindow(d.w0, ev.clientX - d.x0, strip.current!.clientWidth), nowS(), st.current.lim));
  };
  const onPointerUp = (ev: React.PointerEvent) => {
    touches.current.delete(ev.pointerId);
    if (pinch.current) {
      if (touches.current.size < 2) pinch.current = null;   // lifting a finger ends the pinch (no tap to seek)
      drag.current = null;
      return;
    }
    const d = drag.current;
    drag.current = null;
    setDragMode(null);
    if (!d || d.id !== ev.pointerId) return;
    if (d.moving) {
      if (d.mode === "scrub") { setScrubT(null); seek(timeAtX(ev.clientX)); }
      return;
    }
    if (!(ev.target as Element).closest(".ep-tick")) seek(timeAtX(ev.clientX));
  };
  const onPointerCancel = (ev: React.PointerEvent) => {
    touches.current.delete(ev.pointerId);
    pinch.current = null;
    drag.current = null;
    setDragMode(null);
    setScrubT(null);
  };
  // The wheel zooms around the cursor; shift+wheel or a sideways swipe pans. Not passive, so the page doesn't scroll.
  useEffect(() => {
    const el = strip.current;
    if (!el) return;
    const onWheel = (ev: WheelEvent) => {
      ev.preventDefault();
      const r = el.getBoundingClientRect();
      const { lim } = st.current;
      setWin((w) => clampStrip(wheelWindow(w, ev, fracIn(ev.clientX, r), r.width, lim), nowS(), lim));
      endHoverRef.current();   // what is under the cursor changed: the next move shows it
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, []);

  const now = nowS();
  const end = eventEnd(cur, now);
  const evRange = { start: cur.start_ts, end };
  const width = win.end - win.start;
  const zoomOut = zoomStep(evRange, width, 1), zoomIn = zoomStep(evRange, width, -1);
  /** −/+: the next zoom level's width (eventPlayback.ts zoomStep), around the event when the playhead is in it. */
  const zoomTo = (w: number | null) => { if (w != null) setWin(zoomWindowWidth(evRange, w, scrubT ?? tRef.current, nowS())); };
  const onKeyDown = (ev: React.KeyboardEvent) => {
    if (ev.key === "+" || ev.key === "=") { ev.preventDefault(); return zoomTo(zoomIn); }
    if (ev.key === "-") { ev.preventDefault(); return zoomTo(zoomOut); }
    if (ev.key !== "ArrowLeft" && ev.key !== "ArrowRight") return;
    ev.preventDefault();
    const back = ev.key === "ArrowLeft";
    seek(tRef.current + (back ? -KEY_STEP_S : KEY_STEP_S), false, back ? -1 : 1);
  };

  const label = (x: Selected) => `${x.yolo_class ?? x.camera_class} · ${fmtTime(x.start_ts)}${x.status === "rejected" ? " · rejected" : ""}`;
  const marks = ticks(listing.events, win, cur.id, label);
  const all = withSelected(listing.events, cur as TimelineEvent);
  const selected = all.find((x) => x.id === cur.id)!;
  // Previous / next: the listing's neighbour, else the server's nearest beyond it (eventPlayback.ts stepPlan, farNeighbour),
  // asked as soon as it is needed so the button shows what it does; greyed out only when the server says there is none.
  // While a jump waits for its listing, from where it lands.
  const farCache = farCacheFor(site);
  const [, bump] = useReducer((n: number) => n + 1, 0);
  const tStep = jumping?.at ?? t;
  const nowMs = Date.now();
  const button = (dir: -1 | 1): [StepPlan<TimelineEvent>, StepButton<TimelineEvent>] => {
    const plan = stepPlan(all, selected, tStep, dir, now, cam);
    return [plan, stepButton(plan, plan.kind === "far" ? farCache.get(plan.key) : undefined, listing.spans !== undefined, nowMs)];
  };
  const [prevPlan, prev] = button(-1), [nextPlan, next] = button(1);
  const askFar = (plan: StepPlan<TimelineEvent> & { kind: "far" }, force = false) =>
    farCache.ask(plan.key, () => farNeighbour((q) => site.events({ camera: cam, status: STEP_STATUSES, ...q }), plan.anchor, plan.dir, nowS())
      .then((x) => (x ? asTimelineEvent(x) : null)), Date.now(), force);
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const prevAsk = prev.ask && prevPlan.kind === "far" ? prevPlan.key : null, nextAsk = next.ask && nextPlan.kind === "far" ? nextPlan.key : null;
  useEffect(() => {
    const done = () => { if (alive.current) bump(); };
    for (const p of [prevPlan, nextPlan]) if (p.kind === "far" && (p.key === prevAsk || p.key === nextAsk)) askFar(p).then(done, done);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [prevAsk, nextAsk]);
  const curId = useRef(cur.id);
  curId.current = cur.id;
  const stepSeq = useRef(0);
  /** Previous / next pressed: there now, or once the server has answered (if the viewer is still on the same event). */
  const step = (plan: StepPlan<TimelineEvent>, b: StepButton<TimelineEvent>) => {
    const n = ++stepSeq.current;
    if (plan.kind === "listed") return select(plan.event);
    if (b.event) return select(b.event);
    const from = curId.current;
    askFar(plan, true).then((x) => {
      if (!alive.current) return;
      bump();
      if (x && n === stepSeq.current && curId.current === from) select(x);
    }, () => { if (alive.current) bump(); });
  };
  /** Switch the viewer to event `x` of this camera: the strip centres on it at the same zoom, the video goes on from just
   *  before it, and the viewer (onSelectEvent) shows its details. An event beyond the listing (previous / next far
   *  away): its recordings aren't listed yet, so the jump waits for the listing around it (`jumping`). */
  const select = (x: TimelineEvent) => {
    const at = nowS();
    const { source: src, spans: sp, listing: l } = st.current;
    const remount = (src?.kind === "clip" || src?.kind === "still") && Array.isArray(sp) && !anyRecorded(sp, x.start_ts - 5, eventEnd(x, at));
    setPick(x);
    setWin((w) => centreOn(w, x.start_ts, eventEnd(x, at), at));
    if (!remount) {
      const to = jumpTarget(x);
      if (sp === null || (sp !== undefined && to >= l.from && to <= l.to)) seek(to);
      else setJumping({ id: x.id, at: to });
    }
    onSelectEvent?.(x, { remount });
  };
  // ...and once it is there, the jump
  useEffect(() => {
    const j = jumping;
    if (!j || listing.spans === undefined || (listing.spans !== null && (j.at < listing.from || j.at > listing.to))) return;
    seek(j.at);   // (clears `jumping`)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [listing, jumping]);
  const pieces = spans ? coverage(spans, win) : null;
  const gaps = pieces?.some((p) => !p.recorded && p.end - p.start > 2);
  // round times between the end labels: about one per 80 px, none within 95 px of an end (an end label is ~60 px wide)
  const sw = stripW || (isPhone ? 340 : 640);
  const scale = scaleMarks(win, Math.max(0, Math.floor(sw / 80) - 2), Math.min(0.4, 95 / sw));
  const shownT = scrubT ?? t;
  const pct = (x: number) => `${(toFrac(x, win) * 100).toFixed(3)}%`;
  const poster = e.snapshot ? site.media(e, "snapshot.jpg", 960) : undefined;
  const hasVideo = (source?.kind === "recording" && chunk) || source?.kind === "clip";
  // the hover thumbnail: a tick's snapshot, else the recording's frame (not recorded / live: just the time)
  const thumbW = Math.min(THUMB_W, sw);
  const hoverEvent = hover?.tick != null ? listing.events.find((x) => x.id === hover.tick) ?? null : null;
  const hoverGap = hover != null && hoverEvent == null && Array.isArray(spans) && !spanAt(spans, hover.t);
  const hoverShot = hover != null && hoverEvent == null && !hoverGap && hoverFrameTime(hover.t, now) != null && hoverFrames.shot?.cam === cam ? hoverFrames.shot : null;
  const hoverText = hover ? `${clock(hover.t)} · ${relLabel(hover.t, cur.start_ts, end)}` : "";

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
              if (scrubT == null) moveTo(at, false);
              if (chunk.start + chunk.len - at < prefetchLead(chunk.len, PREFETCH_LEAD_S)) prefetchNext(chunk);
            }}
            onError={() => onError(chunk)}
            onEnded={(ev) => advance(chunk, ev.currentTarget)} />
        ) : source?.kind === "clip" ? (
          <video ref={attachVideo} className="clip" src={site.media(e, "clip.mp4")} poster={poster} autoPlay={wantPlay.current} muted={muted} playsInline
            onClick={toggle}
            onTimeUpdate={(ev) => { if (scrubT == null) moveTo(t0 + ev.currentTarget.currentTime, false); }}
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

      <div className="ep-strip-wrap">
        {hover && !dragMode && (hoverEvent || hoverGap || hoverShot) && (
          <div className="tl-thumb ep-thumb" style={{ left: thumbLeft(hover.x, thumbW, sw), width: thumbW }}>
            {hoverEvent ? (
              <div className="ep-thumb-snap">
                <span>No snapshot</span>
                <img src={site.media(hoverEvent, "snapshot.jpg", THUMB_W)} alt="" onError={(ev) => { ev.currentTarget.style.display = "none"; }} />
              </div>
            ) : hoverShot?.url ? <img src={hoverShot.url} alt="" />
              : <div className="tl-thumb-gap">{hoverGap ? "Not recorded" : "No recording"}</div>}
            <div className="tl-thumb-label">{hoverEvent ? label(hoverEvent) : hoverText}</div>
          </div>
        )}
        <div className={`ep-strip${dragMode === "pan" ? " panning" : dragMode === "scrub" ? " scrubbing" : ""}`} ref={strip}
          onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={onPointerUp} onPointerCancel={onPointerCancel}
          onPointerLeave={() => { if (!drag.current) endHover(); }}>
          <div className={`ep-track ${pieces ? "" : "unknown"}`} role="slider" tabIndex={0} onKeyDown={onKeyDown}
            aria-label="Playback position around the event (arrow keys: 5 s; + and −: zoom; Space: play or pause)"
            aria-valuemin={0} aria-valuemax={Math.round(width)} aria-valuenow={Math.round(Math.min(win.end, Math.max(win.start, shownT)) - win.start)}
            aria-valuetext={`${clock(shownT)} (${relLabel(shownT, cur.start_ts, end)})`}>
            {pieces?.map((p) => (
              <div key={p.start} className={p.recorded ? "ep-rec" : "ep-gap"} style={{ left: pct(p.start), width: pct(win.start + p.end - p.start) }} />
            ))}
            {scale.map((m) => <div key={m.t} className="ep-grid" style={{ left: `${(m.frac * 100).toFixed(3)}%` }} />)}
            <div className="ep-event" style={{ left: pct(cur.start_ts), width: `max(6px, ${((toFrac(end, win) - toFrac(cur.start_ts, win)) * 100).toFixed(3)}%)` }} />
            {hover && !dragMode && (
              <div className="ep-hover" style={{ left: `${(toFrac(hover.t, win) * 100).toFixed(3)}%` }}>
                {!(hoverEvent || hoverGap || hoverShot) && <span>{clock(hover.t)}</span>}
              </div>
            )}
          </div>
          {marks.map((m) => (
            <button key={m.id} type="button" tabIndex={-1} data-id={m.id} className={`ep-tick ${m.cls}`}
              style={{ left: `${(m.frac * 100).toFixed(3)}%`, width: `calc(max(6px, ${(m.len * 100).toFixed(3)}%) + 2 * var(--tick-pad))` }}
              aria-label={`Go to ${m.title}`}
              onClick={() => { const x = listing.events.find((ev) => ev.id === m.id); if (x) select(x); }} />
          ))}
          {shownT >= win.start && shownT <= win.end && <div className="ep-playhead" style={{ left: pct(shownT) }}><div className="ep-handle" /></div>}
        </div>
      </div>
      <div className="ep-scale small muted" aria-hidden="true">
        <span className="ep-scale-end">{clock(win.start)}</span>
        {scale.map((m) => <span key={m.t} className="ep-scale-mark" style={{ left: `${(m.frac * 100).toFixed(3)}%` }}>{m.label}</span>)}
        <span className="ep-scale-end">{clock(win.end)}</span>
      </div>
      {gaps ? <div className="ep-legend small muted"><i className="ep-key rec" /> recorded <i className="ep-key gap" /> not recorded</div> : null}
      <div className="ep-controls">
        <button type="button" className="ghost" disabled={prev.disabled} onClick={() => step(prevPlan, prev)}
          aria-label="Previous event on this camera"
          title={prev.event ? `Previous event: ${label(prev.event)}` : prev.disabled ? "No earlier event on this camera" : "Previous event on this camera"}>◀ Prev</button>
        <button type="button" className="ghost" title="Back to the start of this event"
          onClick={() => { const to = jumpTarget(cur); seek(to); setWin(zoomWindowWidth(evRange, width, to, nowS())); }}>Event</button>
        <button type="button" className="ghost" disabled={next.disabled} onClick={() => step(nextPlan, next)}
          aria-label="Next event on this camera"
          title={next.event ? `Next event: ${label(next.event)}` : next.disabled ? "No later event on this camera" : "Next event on this camera"}>Next ▶</button>
        <span className="ep-time"><strong>{clock(shownT)}</strong> <span className="muted">{relLabel(shownT, cur.start_ts, end)}</span></span>
        <div className="ep-zoom" role="group" aria-label={`Strip zoom: ${halfLabel(width / 2)} (scroll on the strip to zoom, drag to pan)`}>
          <button type="button" className="ghost" disabled={zoomOut == null} onClick={() => zoomTo(zoomOut)}
            aria-label="Zoom the strip out" title="Zoom out: more time on the strip (or scroll on it)">−</button>
          <span className="small muted">{halfLabel(width / 2)}</span>
          <button type="button" className="ghost" disabled={zoomIn == null} onClick={() => zoomTo(zoomIn)}
            aria-label="Zoom the strip in" title="Zoom in: less time on the strip (or scroll on it)">+</button>
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
