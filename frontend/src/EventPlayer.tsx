import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fmtTime, type NvrEvent, type PlaybackQuality, type SiteApi, type TimelineEvent } from "./api";
import {
  KEY_STEP_S, NEIGHBOUR_RANGE_S, afterPlaybackError, atFrac, chunkAt, clipOrigin, clock, coverage, defaultQuality, eventEnd,
  followWindow, footageAvailable, initialSource, jumpTarget, neighbour, nextChunkStart, playStart, relLabel, snapToRecorded,
  stripWindow, ticks, toFrac, viaHub, type Source, type Win,
} from "./eventPlayback";
import { httpStatus } from "./liveFallback";
import { PREFETCH_LEAD_S, dropPrefetch, isBuffered, mergeSpans, nowS, prefetchChunk, spanAt, useLatestFrame, type Prefetched, type Span } from "./playback";
import { useIsPhone } from "./ui";

/** What the event viewer can do with the player: seek it (Ask answers' frames, notes) to epoch `t`, paused if asked. */
export type EventPlayerHandle = { seek: (t: number, pause?: boolean) => void };

/** Can the server transcode playback to SD (q=sd)? Asked once per server client per page load; an older server says no. */
const sdCaps = new WeakMap<SiteApi, Promise<boolean>>();
const canSd = (site: SiteApi): Promise<boolean> => {
  let p = sdCaps.get(site);
  if (!p) {
    p = site.playbackCapabilities().then((c) => !!c?.sd).catch(() => false);
    sdCaps.set(site, p);
  }
  return p;
};

type Listing = { spans: Span[] | null | undefined; events: TimelineEvent[]; from: number; to: number };
type Chunk = { start: number; len: number; src: string; key: number };

/**
 * The event viewer's video: the camera's recording from shortly before the event, through it and on, chunk after chunk
 * as the Timeline plays (playback.ts), with a strip under it of about ten minutes around the event: what is recorded
 * (gaps hatched: older events often keep only the footage retention kept around them), the event, the camera's other
 * events (tap one to go there), a playhead to drag or tap, and previous / next / back to the event.
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
  const [stopped, setStopped] = useState<"error" | "end" | "live" | null>(null);
  const video = useRef<HTMLVideoElement | null>(null);
  const tRef = useRef(from);
  const wantPlay = useRef(!startAt?.paused);   // the viewer's play / pause (native controls), kept across chunks
  const played = useRef(false);                // some recording chunk played: a later failure stops there, no fallback
  const prefetch = useRef<Prefetched | null>(null);
  const blobInUse = useRef<string | null>(null);
  const firstChunk = useRef(true);             // the snapshot is the poster of the first chunk only, not of every switch
  const st = useRef({ spans, quality, remote, chunk, source, win });
  st.current = { spans, quality, remote, chunk, source, win };

  useEffect(() => { canSd(site).then(setCaps); }, [site]);

  // The recordings listing (and the camera's events) for the window, with NEIGHBOUR_RANGE_S either side so previous /
  // next can reach past it; listed again only when the window leaves what was listed.
  const list = useCallback((w: Win) => {
    const lo = w.start - NEIGHBOUR_RANGE_S, hi = Math.min(w.end + NEIGHBOUR_RANGE_S, nowS());
    return site.recordings(cam, lo, hi)
      .then((r) => ({ spans: mergeSpans(r.spans), events: r.events, from: lo, to: hi }))
      .catch(() => ({ spans: null, events: [], from: lo, to: hi }));
  }, [site, cam]);
  useEffect(() => {
    if (listing.spans !== undefined && win.start >= listing.from && (win.end <= listing.to || listing.to >= nowS() - 60)) return;
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

  /** Start a recording chunk at `at`: the prefetched one when it is that, else a request (`cont` = a continuation). */
  const load = (at: number, cont = false) => {
    const { spans, quality, remote } = st.current;
    if (quality == null) return;
    if (blobInUse.current) URL.revokeObjectURL(blobInUse.current);
    blobInUse.current = null;
    let { len } = chunkAt(at, spans ?? null, { remote, cont, now: nowS() });
    let src = site.playbackUrl(cam, at, len, quality);
    const p = prefetch.current;
    if (p && Math.abs(p.start - at) < 1 && p.url) {
      len = p.len;
      src = p.url;
      blobInUse.current = p.url;
      prefetch.current = null;           // handed over; not revoked
    } else {
      dropPrefetch(p);
      prefetch.current = null;
    }
    setStopped(null);
    setSource({ kind: "recording", at });
    setChunk({ start: at, len, src, key: Date.now() });
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

  /** Go to `to`: within the chunk when it is there already, else a new chunk; a gap goes to the next recording (`dir`
   *  -1: the end of the one before). */
  const seek = (to: number, pause = false, dir: -1 | 1 = 1) => {
    if (pause) { wantPlay.current = false; video.current?.pause(); }
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
      return moveTo(at);
    }
    load(at);
  };
  useEffect(() => { if (handle) handle.current = { seek }; });
  useEffect(() => () => { if (handle) handle.current = null; }, [handle]);   // hidden (a phone's other tab): nothing to seek

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
  const label = (x: TimelineEvent) => `${x.yolo_class ?? x.camera_class} · ${fmtTime(x.start_ts)}${x.status === "rejected" ? " · rejected" : ""}`;
  const marks = ticks(listing.events, win, e.id, label);
  const all = listing.events.some((x) => x.id === e.id) ? listing.events : [...listing.events, { ...e, verdict: null, has_synopsis: 0 } as TimelineEvent];
  const prev = neighbour(all, t, -1), next = neighbour(all, t, 1);
  const pieces = spans ? coverage(spans, win) : null;
  const gaps = pieces?.some((p) => !p.recorded && p.end - p.start > 2);
  const shownT = scrubT ?? t;
  const pct = (x: number) => `${(toFrac(x, win) * 100).toFixed(3)}%`;
  const poster = e.snapshot ? site.media(e, "snapshot.jpg", 960) : undefined;

  const onError = async (c: Chunk) => {
    const status = c.src.startsWith("blob:") ? null : await httpStatus(c.src);
    if (st.current.chunk?.key !== c.key) return;   // already replaced
    const step = afterPlaybackError({ status, quality: st.current.quality ?? "hd", played: played.current, hasClip });
    if (step === "hd") setSdBusy(true);            // quietly: the quality effect reloads this place in HD
    else if (step === "stop") setStopped("error");
    else { setChunk(null); setSource({ kind: step }); }
  };
  const onEnded = async (c: Chunk, v: HTMLVideoElement) => {
    const d = v.duration;
    const playedS = Number.isFinite(d) && d > 0 ? d : c.len;
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

  return (
    <div className="ep">
      <div className="ep-video">
        {source?.kind === "recording" && chunk ? (
          <video key={chunk.key} ref={video} className="clip" src={chunk.src} poster={firstChunk.current ? poster : undefined}
            controls muted playsInline
            onLoadedMetadata={(ev) => { if (wantPlay.current) ev.currentTarget.play().catch(() => {}); }}
            onPlaying={() => { played.current = true; firstChunk.current = false; caughtUp(); }}
            onSeeked={caughtUp}
            onPlay={() => { wantPlay.current = true; }}
            onPause={(ev) => { if (!ev.currentTarget.ended) wantPlay.current = false; }}
            onTimeUpdate={(ev) => {
              const at = chunk.start + ev.currentTarget.currentTime;
              if (scrubT == null) moveTo(at);
              if (chunk.start + chunk.len - at < PREFETCH_LEAD_S) prefetchNext(chunk);
            }}
            onError={() => onError(chunk)}
            onEnded={(ev) => onEnded(chunk, ev.currentTarget)} />
        ) : source?.kind === "clip" ? (
          <video ref={video} className="clip" src={site.media(e, "clip.mp4")} poster={poster} controls autoPlay={wantPlay.current} muted playsInline
            onTimeUpdate={(ev) => { if (scrubT == null) moveTo(t0 + ev.currentTarget.currentTime); }}
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
        {stopped && !frames.shot && (
          <div className="ep-msg ep-msg-bar">
            {stopped === "error" ? "The recording couldn't be loaded here" : stopped === "live" ? "Caught up with live" : "End of the recording"}
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
          aria-label="Playback position around the event (arrow keys: 5 s)"
          aria-valuemin={0} aria-valuemax={Math.round(win.end - win.start)} aria-valuenow={Math.round(Math.min(win.end, Math.max(win.start, shownT)) - win.start)}
          aria-valuetext={`${clock(shownT)} (${relLabel(shownT, e.start_ts, end)})`}>
          {pieces?.map((p) => (
            <div key={p.start} className={p.recorded ? "ep-rec" : "ep-gap"} style={{ left: pct(p.start), width: pct(win.start + p.end - p.start) }}
              title={p.recorded ? `Recorded ${clock(p.start)}–${clock(p.end)}` : `Not recorded ${clock(p.start)}–${clock(p.end)}`} />
          ))}
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
      <div className="ep-axis small muted">
        <span>{clock(win.start)}</span>
        {gaps ? <span className="ep-legend"><i className="ep-key rec" /> recorded <i className="ep-key gap" /> not recorded</span> : null}
        <span>{clock(win.end)}</span>
      </div>
      <div className="ep-controls">
        <button type="button" className="ghost" disabled={!prev} onClick={() => prev && seek(jumpTarget(prev))}
          aria-label="Previous event on this camera" title={prev ? `Previous event: ${label(prev)}` : "No earlier event on this camera"}>◀ Prev</button>
        <button type="button" className="ghost" onClick={() => seek(jumpTarget(e))} title="Back to the start of this event">Event</button>
        <button type="button" className="ghost" disabled={!next} onClick={() => next && seek(jumpTarget(next))}
          aria-label="Next event on this camera" title={next ? `Next event: ${label(next)}` : "No later event on this camera"}>Next ▶</button>
        <span className="ep-time"><strong>{clock(shownT)}</strong> <span className="muted">{relLabel(shownT, e.start_ts, end)}</span></span>
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
