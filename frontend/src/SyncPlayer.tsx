import { useEffect, useRef, useState } from "react";
import { playbackUrl } from "./api";
import { CHUNK, isBuffered, spanAt, useLatestFrame, type Span } from "./playback";

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
  onSolo, onSelect, statusRef,
}: {
  cam: string; name: string; spans: Span[] | undefined; clockRef: React.RefObject<number | null>;
  playing: boolean; speed: number; scrubbing: boolean; scrubT: number | null; previewWidth: number;
  active: boolean; soloed: boolean; onSolo: () => void; onSelect: () => void;
  statusRef: React.RefObject<Record<string, TileStatus>>;
}) {
  const video = useRef<HTMLVideoElement>(null);
  const [chunk, setChunk] = useState<{ start: number; key: number } | null>(null);
  const loaded = useRef(false);
  const [status, setStatus] = useState<TileStatus>("idle");
  const frames = useLatestFrame(previewWidth);
  const props = useRef({ playing, speed, spans, scrubbing });
  props.current = { playing, speed, spans, scrubbing };
  const chunkRef = useRef(chunk);
  chunkRef.current = chunk;

  const report = (s: TileStatus) => {
    statusRef.current![cam] = s;
    setStatus((prev) => (prev === s ? prev : s));
  };
  const load = (t: number) => {
    loaded.current = false;
    setChunk({ start: t, key: Date.now() });
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
      if (!c || t < c.start - DRIFT_S || t > c.start + CHUNK - 1) {
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
      report(!playing ? "paused" : v.readyState >= 3 && !v.paused ? "playing" : "buffering");
    };
    tick();
    const id = window.setInterval(tick, TICK_MS);
    return () => {
      window.clearInterval(id);
      delete statusRef.current![cam];
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

  return (
    <div className={`sync-tile ${active ? "active" : ""}`} onClick={onSelect} onDoubleClick={onSolo}>
      {chunk && (
        <video
          key={chunk.key}
          ref={video}
          src={playbackUrl(cam, chunk.start, CHUNK)}
          muted
          playsInline
          onLoadedMetadata={(e) => {
            loaded.current = true;
            e.currentTarget.currentTime = Math.max(0, (clockRef.current ?? chunk.start) - chunk.start);
          }}
          onSeeked={onCaughtUp}
          onPlaying={onCaughtUp}
          onEnded={(e) => {
            const next = chunk.start + (e.currentTarget.duration || CHUNK) + 0.1;
            if (spanAt(props.current.spans, next)) load(next);
          }}
        />
      )}
      {frames.shot && (
        <div className="sync-preview">
          {frames.shot.url ? <img src={frames.shot.url} alt="" /> : <div className="sync-gap">No recording here</div>}
        </div>
      )}
      {status === "gap" && !frames.shot && <div className="sync-gap">No recording at this time</div>}
      <div className="sync-bar">
        <span className="sync-name">{name}</span>
        {label && <span className={`sync-status ${status}`}>{label}</span>}
        <span className="spacer" />
        <button className={`ghost small sync-solo ${soloed ? "on" : ""}`} title={soloed ? "Back to the grid (0)" : "Isolate this camera"}
          onClick={(e) => { e.stopPropagation(); onSolo(); }}>🔍</button>
      </div>
    </div>
  );
}
