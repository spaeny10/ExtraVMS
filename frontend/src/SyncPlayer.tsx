import { RegionBadge, RegionOverlay } from "./RegionPaint";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Camera, type SiteApi } from "./api";
import { PREFETCH_LEAD_S, chunkLen, dropPrefetch, firstChunkLen, isBuffered, prefetchChunk, spanAt, useLatestFrame, type Prefetched, type Span } from "./playback";

export type TileStatus = "idle" | "paused" | "playing" | "buffering" | "gap";
const DRIFT_S = 0.5;      // paused: re-seek a tile further than this from the shared clock
const TRIM_DRIFT_S = 0.15; // playing: nudge the rate when further than this
const SEEK_DRIFT_S = 6;    // playing: seek/reload when further than this
const TICK_MS = 250;

/**
 * One camera in the synced Timeline grid. It follows a shared clock (clockRef.current = epoch seconds):
 * loads the recording chunk around the clock time, corrects drift, pauses in gaps, and shows
 * frame previews while the playhead is being scrubbed.
 */
export function SyncTile({
  cam, name, spans, clockRef, playing, speed, scrubbing, scrubT, previewWidth, active, soloed,
  onSolo, onSelect, statusRef, dragging, dropTarget, onDragPointerDown, camera, hasAudio, audioOn, onToggleAudio,
  site = api, camId, timeOffsetS = 0,
}: {
  cam: string; name: string; spans: Span[] | undefined; clockRef: React.RefObject<number | null>;
  playing: boolean; speed: number; scrubbing: boolean; scrubT: number | null; previewWidth: number;
  active: boolean; soloed: boolean; onSolo: () => void; onSelect: () => void;
  dragging?: boolean; dropTarget?: boolean;
  /** the camera records an audio track; one tile at a time may play it (the others stay muted) */
  hasAudio?: boolean; audioOn?: boolean; onToggleAudio?: () => void;
  /** pointerdown that may become a reorder drag; fromGrip = started on the ⠿ handle (touch-friendly) */
  onDragPointerDown?: (e: React.PointerEvent, fromGrip: boolean) => void;
  /** the camera record, so a painted region can be saved as a named place */
  camera?: Camera;
  statusRef: React.RefObject<Record<string, TileStatus>>;
  /** the camera's server (default: this server); `cam` stays the lane key, `camId` is the id on that server */
  site?: SiteApi; camId?: string;
  /** how far this server's clock is ahead of the shared clock (server time = shared + offset; 0 = none) */
  timeOffsetS?: number;
}) {
  const id = camId ?? cam;
  const off = timeOffsetS;
  const frameUrlFor = useCallback((_k: string, t: number, w?: number, exact?: boolean) => site.frameUrl(id, t + off, w, exact), [site, id, off]);
  const video = useRef<HTMLVideoElement>(null);
  const [chunk, setChunk] = useState<{ start: number; key: number; len: number; src: string } | null>(null);
  const loaded = useRef(false);
  const prefetch = useRef<Prefetched | null>(null);   // the chunk after the current one, downloading ahead of need
  const blobInUse = useRef<string | null>(null);      // the current chunk's blob URL, revoked when it is replaced
  const [status, setStatus] = useState<TileStatus>("idle");
  const frames = useLatestFrame(previewWidth, frameUrlFor);
  const props = useRef({ playing, speed, spans, scrubbing });
  props.current = { playing, speed, spans, scrubbing };
  const chunkRef = useRef(chunk);
  chunkRef.current = chunk;

  const report = (s: TileStatus) => {
    statusRef.current![cam] = s;
    setStatus((prev) => (prev === s ? prev : s));
  };
  /** Start a chunk at `t`. A seek (`cont` false) far back uses a short first chunk so video appears within a
   *  second; a continuation (`cont` true) uses the full length and the prefetched download when it has one. */
  const load = (t: number, cont = false) => {
    loaded.current = false;
    if (blobInUse.current) URL.revokeObjectURL(blobInUse.current);
    blobInUse.current = null;
    let len = cont ? chunkLen(t) : firstChunkLen(t);
    let src = site.playbackUrl(id, t + off, len);
    const p = prefetch.current;
    if (p && Math.abs(p.start - t) < 1 && p.url) {
      len = p.len;
      src = p.url;
      blobInUse.current = p.url;
      prefetch.current = null;           // handed over; not revoked
    } else {
      dropPrefetch(p);                   // stale or unfinished: a new one starts once this chunk plays
      prefetch.current = null;
    }
    setChunk({ start: t, key: Date.now(), len, src });
  };
  /** The current chunk is running out: download the next one now so the switch is seamless. */
  const prefetchNext = (c: { start: number; len: number }) => {
    const next = c.start + c.len + 0.1;
    if (prefetch.current || !spanAt(props.current.spans, next)) return;
    const len = chunkLen(next);
    prefetch.current = prefetchChunk(site.playbackUrl(id, next + off, len), next, len);
  };

  // Follow the shared clock.
  useEffect(() => {
    const tick = () => {
      const { playing, speed, spans, scrubbing } = props.current;
      const t = clockRef.current;
      const v = video.current;
      const c = chunkRef.current;
      if (t == null) return report("idle");
      if (!spanAt(spans, t)) {
        if (v && !v.paused) v.pause();
        return report("gap");
      }
      if (scrubbing) return report("paused"); // the preview frames are showing; don't fight the drag
      if (!c || t < c.start - DRIFT_S || t > c.start + c.len - 1) {
        load(t);
        return report(playing ? "buffering" : "paused");
      }
      if (!v || !loaded.current) return report(playing ? "buffering" : "paused");
      const rel = t - c.start;
      const drift = v.currentTime - rel; // > 0: this tile is ahead of the clock
      let rate = speed;
      if (Math.abs(drift) > SEEK_DRIFT_S || (!playing && Math.abs(drift) > DRIFT_S)) {
        // Large gap (or paused: nothing to catch up with): jump.
        if (isBuffered(v, rel, 0.1)) v.currentTime = rel;
        else {
          const end = v.buffered.length ? v.buffered.end(v.buffered.length - 1) : 0;
          if (rel > end + 4 || rel < (v.buffered.length ? v.buffered.start(0) : 0)) {
            load(t); // far outside what this chunk has: fetch a new one at the clock time
            return report(playing ? "buffering" : "paused");
          }
        }
      } else if (Math.abs(drift) > TRIM_DRIFT_S) {
        // Small gap: nudge the playback rate. The browser only buffers a few seconds ahead,
        // so a lagging tile can't simply seek forward; playing slightly faster closes the gap.
        const nudge = Math.min(0.3, Math.abs(drift) * 0.25);
        rate = speed * (drift < 0 ? 1 + nudge : 1 - nudge);
      }
      if (Math.abs(v.playbackRate - rate) > 0.01) v.playbackRate = rate;
      if (playing && v.paused) v.play().catch(() => {});
      if (!playing && !v.paused) v.pause();
      if (playing && c.start + c.len - t < PREFETCH_LEAD_S * Math.max(1, speed)) prefetchNext(c);
      report(!playing ? "paused" : v.readyState >= 3 && !v.paused ? "playing" : "buffering");
    };
    tick();
    const id = window.setInterval(tick, TICK_MS);
    return () => {
      window.clearInterval(id);
      delete statusRef.current![cam];
      dropPrefetch(prefetch.current);
      prefetch.current = null;
      if (blobInUse.current) URL.revokeObjectURL(blobInUse.current);
      blobInUse.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cam]);

  // Scrub previews: frames at the dragged time; refine to the exact frame when the cursor rests.
  useEffect(() => {
    if (!scrubbing || scrubT == null) return;
    frames.request(cam, scrubT);
    const id = window.setTimeout(() => frames.request(cam, scrubT, true), 200);
    return () => window.clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scrubbing, scrubT, cam]);
  // After a scrub, keep the preview until the video has caught up, then drop it.
  useEffect(() => {
    if (scrubbing || !frames.shot) return;
    frames.stop();
    const id = window.setTimeout(frames.clear, 4000);
    return () => window.clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scrubbing]);

  const onCaughtUp = () => {
    if (!props.current.scrubbing && frames.shot) frames.clear();
  };

  const label = { idle: "", paused: "", playing: "", buffering: "Buffering…", gap: "No recording" }[status];
  const [painting, setPainting] = useState(false);

  return (
    <div className={`sync-tile ${active ? "active" : ""} ${dragging ? "dragging" : ""} ${dropTarget ? "drop-target" : ""} ${painting ? "painting" : ""}`}
      data-cam={cam} onClick={onSelect} onDoubleClick={onSolo} title="Drag to reorder · double-click to isolate"
      onPointerDown={(e) => onDragPointerDown?.(e, false)}>
      {chunk && (
        <video
          key={chunk.key}
          ref={video}
          src={chunk.src}
          muted={!audioOn}
          playsInline
          onLoadedMetadata={(e) => {
            loaded.current = true;
            e.currentTarget.currentTime = Math.max(0, (clockRef.current ?? chunk.start) - chunk.start);
          }}
          onSeeked={onCaughtUp}
          onPlaying={onCaughtUp}
          onEnded={(e) => {
            const d = e.currentTarget.duration;
            const next = chunk.start + (Number.isFinite(d) && d > 0 ? d : chunk.len) + 0.1;
            if (spanAt(props.current.spans, next)) load(next, true);
          }}
        />
      )}
      {frames.shot && (
        <div className="sync-preview">
          {frames.shot.url ? <img src={frames.shot.url} alt="" /> : <div className="sync-gap">No recording here</div>}
        </div>
      )}
      {status === "gap" && !frames.shot && <div className="sync-gap">No recording at this time</div>}
      <RegionOverlay cam={cam} videoRef={video} editing={painting} onDone={() => setPainting(false)} camera={camera} site={site} />
      <div className="sync-bar">
        {onDragPointerDown && <span className="sync-grip" title="Drag to reorder" onPointerDown={(e) => { e.stopPropagation(); onDragPointerDown(e, true); }}>⠿</span>}
        <span className="sync-name">{name}</span>
        {label && <span className={`sync-status ${status}`}>{label}</span>}
        <span className="spacer" />
        {!painting && <RegionBadge cam={cam} onEdit={() => setPainting(true)} />}
        {hasAudio && onToggleAudio && (
          <button className={`ghost small sync-audio ${audioOn ? "on" : ""}`} title={audioOn ? "Mute" : "Play this camera's sound (one camera at a time)"}
            onClick={(e) => { e.stopPropagation(); onToggleAudio(); }}>{audioOn ? "🔊" : "🔇"}</button>
        )}
        <button className={`ghost small sync-paint ${painting ? "on" : ""}`} title="Paint a region: show only events that passed through it"
          onClick={(e) => { e.stopPropagation(); setPainting((p) => !p); }}>✎</button>
        <button className={`ghost small sync-solo ${soloed ? "on" : ""}`} title={soloed ? "Back to the grid (0)" : "Isolate this camera"}
          onClick={(e) => { e.stopPropagation(); onSolo(); }}>🔍</button>
      </div>
    </div>
  );
}
