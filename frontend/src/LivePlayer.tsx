/**
 * Live picture for a camera tile: WebRTC (WhepPlayer) with the ICE servers the app was given (a TURN relay
 * when viewed through the fleet hub), falling back to chained fMP4 recordings a few seconds behind live
 * when WebRTC can't connect — e.g. a network that blocks UDP and the relay alike. A camera with nothing recorded
 * for "now" says so and tries WebRTC again every 30 s, instead of bouncing between the two every few seconds.
 *
 * A new `path` (SD ↔ HD) switches seamlessly (liveSwap.ts): the new stream connects out of sight (transparent, over
 * the old one: not display:none, where the first-frame callback may never fire) while the old one keeps playing, and
 * replaces it on its first frame; the old one then hangs up (WHEP DELETE). A new stream that fails, or shows nothing
 * within 10 s, is dropped and the old path stays: onSwitchFailed tells the caller, which should go back to that path.
 */
import { useEffect, useMemo, useReducer, useRef, useState, type ReactNode } from "react";
import { api, makeApi, type SiteApi } from "./api";
import { NO_RECORDING_RETRY_MS, NO_RECORDING_TEXT, afterFollowError, httpStatus } from "./liveFallback";
import { SWAP_TIMEOUT_MS, swapInit, swapReducer, swapStreams } from "./liveSwap";
import { CHUNK, LIVE_LAG, nowS } from "./playback";
import { WhepPlayer, type WhepState } from "./WhepPlayer";
import { type WhepStatsSample, type WhepTimingSnapshot, pathQuality } from "./whepTiming";
import { ZoomFrame } from "./VideoZoom";

const FALLBACK_LAG = LIVE_LAG + 6;   // a chunk must exist on disk before it can be fetched

/** For `pathClass`: "hd" (the HD box shape) on main-stream paths, per stream, so the old picture keeps its shape while
 *  the new one connects. */
export const hdPathClass = (path: string) => (pathQuality(path) === "hd" ? "hd" : "");

type LivePlayerProps = {
  path: string; port: number; className?: string; showSize?: boolean;
  /** this browser can't decode `path` (WhepPlayer's onUnsupported), whether it was on screen or connecting for a switch */
  onUnsupported?: (path: string) => void;
  /** an extra class per stream, from its path (hdPathClass) */
  pathClass?: (path: string) => string;
  /** the camera whose recording the fallback follows; default: the path without _sub / _hd */
  camera?: string;
  iceServers?: RTCIceServer[]; videoRef?: React.MutableRefObject<HTMLVideoElement | null>; children?: ReactNode;
  /** connection state of the WebRTC player (the recording fallback counts as "playing") */
  onState?: (s: WhepState) => void;
  /** URL prefix of the camera's site ("/s/<site>" on the hub dashboard); default: this page's site */
  base?: string;
  /** the camera's server client; wins over `base` (the hub's direct client carries a token a bare base can't) */
  site?: SiteApi;
  muted?: boolean; onAudio?: (has: boolean) => void;
  /** passed to the WebRTC player (WhepPlayer): the first frame's timing, and link quality every 2 s of the stream on screen */
  onFirstFrame?: (timing: WhepTimingSnapshot) => void; onStats?: (sample: WhepStatsSample) => void;
  /** a switch to this path failed: the old path still plays */
  onSwitchFailed?: (path: string) => void;
};

export function LivePlayer({ path, className, pathClass, videoRef, children, muted, onAudio, onState, onFirstFrame, onStats, onSwitchFailed, ...rest }: LivePlayerProps) {
  const [swap, dispatch] = useReducer(swapReducer, path, (p) => swapInit(p, performance.now()));
  useEffect(() => { dispatch({ type: "path", path, now: performance.now() }); }, [path]);
  const nextId = swap.next?.id;
  useEffect(() => {
    if (nextId === undefined) return;
    const t = setTimeout(() => dispatch({ type: "tick", now: performance.now() }), SWAP_TIMEOUT_MS + 50);
    return () => clearTimeout(t);
  }, [nextId]);
  const failedCb = useRef(onSwitchFailed);
  failedCb.current = onSwitchFailed;
  useEffect(() => { if (swap.failed) failedCb.current?.(swap.failed); }, [swap.failed]);
  // each stream's audio; the caller hears about the one on screen, also when a new one takes over
  const audio = useRef<Record<number, boolean>>({});
  const audioCb = useRef(onAudio);
  audioCb.current = onAudio;
  const activeId = swap.active.id;
  const activeRef = useRef(activeId);
  activeRef.current = activeId;
  const wanted = useRef(path);
  wanted.current = path;
  useEffect(() => { if (audio.current[activeId] !== undefined) audioCb.current?.(audio.current[activeId]); }, [activeId]);

  return (
    <div className="live-swap">
      {swapStreams(swap).map((st) => {
        const on = st.id === activeId;
        return (
          <LiveStream key={st.id} {...rest} path={st.path} hidden={st.hidden} poster={!st.swapIn}
            className={[className, pathClass?.(st.path), st.hidden ? "player-next" : ""].filter(Boolean).join(" ")}
            videoRef={on ? videoRef : undefined} muted={on ? muted : true} onState={on ? onState : undefined}
            onAudio={(has) => { audio.current[st.id] = has; if (st.id === activeRef.current) audioCb.current?.(has); }}
            onFirstFrame={(t) => { dispatch({ type: "frame", id: st.id }); onFirstFrame?.(t); }}
            // a decoded frame in the stats counts too: a stream out of sight may never get its frame callback. The caller
            // gets the samples of the stream on screen, once it is the path asked for (not the old one during a switch)
            onStats={(x) => {
              if (x.framesDecoded > 0) dispatch({ type: "frame", id: st.id });
              if (st.id === activeRef.current && st.path === wanted.current) onStats?.(x);
            }}
            onFail={() => dispatch({ type: "fail", id: st.id })}>
            {on ? children : null}
          </LiveStream>
        );
      })}
    </div>
  );
}

/** One path's picture: WebRTC, else the recording, else "No recording" (see LivePlayer). A stream connecting `hidden`
 *  for a switch never falls back: it fails instead (onFail), and the stream on screen stays. */
function LiveStream({ path, camera, port, className, onUnsupported, showSize, iceServers, videoRef, children, base, site, muted, onAudio, onState,
  onFirstFrame, onStats, hidden, poster, onFail }: Omit<LivePlayerProps, "pathClass" | "onSwitchFailed"> & { hidden: boolean; poster: boolean; onFail: () => void }) {
  // webrtc → (repeated failures) → recording → (its chunk is a 404) → none → (30 s) → webrtc
  const [mode, setMode] = useState<"webrtc" | "recording" | "none">("webrtc");
  useEffect(() => {
    if (mode !== "none") return;
    const t = setTimeout(() => { onState?.("connecting"); setMode("webrtc"); }, NO_RECORDING_RETRY_MS);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode]);
  if (mode === "webrtc") {
    // camera: the still shown while connecting comes from this camera's recording
    return <WhepPlayer path={path} camera={camera} port={port} className={className} showSize={showSize && !hidden} base={base} site={site} poster={poster}
      onUnsupported={hidden || onUnsupported ? () => { if (hidden) onFail(); onUnsupported?.(path); } : undefined}
      iceServers={iceServers} videoRef={videoRef} muted={muted} onAudio={onAudio} onState={onState} onFirstFrame={onFirstFrame} onStats={onStats}
      onFallback={() => { if (hidden) onFail(); else { onState?.("playing"); setMode("recording"); } }}>{children}</WhepPlayer>;
  }
  if (mode === "none") {
    return (
      <div className={`player ${className ?? ""}`}>
        <div className="player-state" role="status" title="WebRTC unavailable and nothing recorded for now: trying live again every 30 s">{NO_RECORDING_TEXT}</div>
        {children}
      </div>
    );
  }
  return <RecordingFollow camera={camera ?? path.replace(/_(sub|hd)$/, "")} className={className} videoRef={videoRef} base={base} site={site}
    onGiveUp={() => { onState?.("connecting"); setMode("webrtc"); }} onNoRecording={() => { onState?.("error"); setMode("none"); }}>{children}</RecordingFollow>;
}

/**
 * Plays the recording from a few seconds ago and keeps loading the next chunk: live-ish without WebRTC. A chunk that
 * fails is checked for its HTTP status: 404 (nothing recorded) ends it at once (onNoRecording); other failures are
 * retried after 3 s, and after 3 in a row without a chunk playing in between it hands back to WebRTC (onGiveUp).
 */
function RecordingFollow({ camera, className, videoRef, onGiveUp, onNoRecording, children, base, site: given }: {
  camera: string; className?: string; videoRef?: React.MutableRefObject<HTMLVideoElement | null>; onGiveUp: () => void; onNoRecording: () => void;
  children?: ReactNode; base?: string; site?: SiteApi;
}) {
  const site = useMemo(() => given ?? (base === undefined ? api : makeApi(base)), [given, base]);
  const ref = useRef<HTMLVideoElement | null>(null);
  const [start, setStart] = useState(() => nowS() - FALLBACK_LAG);
  const errors = useRef(0);
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const set = (el: HTMLVideoElement | null) => { ref.current = el; if (videoRef) videoRef.current = el; };
  const url = site.playbackUrl(camera, start, CHUNK);
  const failed = async () => {
    const n = ++errors.current;
    // a <video> error doesn't say why: ask the same URL for its status (headers only)
    const step = afterFollowError(await httpStatus(url), n);
    if (!alive.current) return;
    if (step === "no-recording") onNoRecording();
    else if (step === "give-up") onGiveUp();
    else setTimeout(() => { if (alive.current) setStart(nowS() - FALLBACK_LAG); }, 3000);
  };
  return (
    <div className={`player ${className ?? ""}`}>
      <ZoomFrame>
        <video ref={set} key={start} src={url} autoPlay muted playsInline
          onPlaying={() => { errors.current = 0; }}
          onEnded={() => setStart((s) => Math.min(s + CHUNK, nowS() - FALLBACK_LAG))}
          onError={() => { void failed(); }} />
      </ZoomFrame>
      <div className="player-size" title="WebRTC unavailable: playing the recording a few seconds behind live">recording · {FALLBACK_LAG}s behind</div>
      {children}
    </div>
  );
}
