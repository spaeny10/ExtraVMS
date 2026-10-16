/**
 * Live picture for a camera tile: WebRTC (WhepPlayer) with the ICE servers the app was given (a TURN relay
 * when viewed through the fleet hub), falling back to chained fMP4 recordings a few seconds behind live
 * when WebRTC can't connect — e.g. a network that blocks UDP and the relay alike. A camera with nothing recorded
 * for "now" says so and tries WebRTC again every 30 s, instead of bouncing between the two every few seconds.
 */
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api, makeApi, type SiteApi } from "./api";
import { NO_RECORDING_RETRY_MS, NO_RECORDING_TEXT, afterFollowError, httpStatus } from "./liveFallback";
import { CHUNK, LIVE_LAG, nowS } from "./playback";
import { WhepPlayer, type WhepState } from "./WhepPlayer";
import { ZoomFrame } from "./VideoZoom";

const FALLBACK_LAG = LIVE_LAG + 6;   // a chunk must exist on disk before it can be fetched

export function LivePlayer({ path, camera, port, className, onUnsupported, showSize, iceServers, videoRef, children, base, site, muted, onAudio, onState }: {
  path: string; port: number; className?: string; onUnsupported?: () => void; showSize?: boolean;
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
}) {
  // webrtc → (repeated failures) → recording → (its chunk is a 404) → none → (30 s) → webrtc
  const [mode, setMode] = useState<"webrtc" | "recording" | "none">("webrtc");
  useEffect(() => { setMode("webrtc"); }, [path]);
  useEffect(() => {
    if (mode !== "none") return;
    const t = setTimeout(() => { onState?.("connecting"); setMode("webrtc"); }, NO_RECORDING_RETRY_MS);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode]);
  if (mode === "webrtc") {
    // camera: the still shown while connecting comes from this camera's recording
    return <WhepPlayer path={path} camera={camera} port={port} className={className} onUnsupported={onUnsupported} showSize={showSize} base={base} site={site}
      iceServers={iceServers} videoRef={videoRef} muted={muted} onAudio={onAudio} onState={onState} onFallback={() => { onState?.("playing"); setMode("recording"); }}>{children}</WhepPlayer>;
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
