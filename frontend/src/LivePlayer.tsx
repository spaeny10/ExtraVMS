/**
 * Live picture for a camera tile: WebRTC (WhepPlayer) with the ICE servers the app was given (a TURN relay
 * when viewed through the fleet hub), falling back to chained fMP4 recordings a few seconds behind live
 * when WebRTC can't connect — e.g. a network that blocks UDP and the relay alike.
 */
import { useEffect, useRef, useState, type ReactNode } from "react";
import { playbackUrl } from "./api";
import { CHUNK, LIVE_LAG, nowS } from "./playback";
import { WhepPlayer } from "./WhepPlayer";

const FALLBACK_LAG = LIVE_LAG + 6;   // a chunk must exist on disk before it can be fetched

export function LivePlayer({ path, port, className, onUnsupported, showSize, iceServers, videoRef, children }: {
  path: string; port: number; className?: string; onUnsupported?: () => void; showSize?: boolean;
  iceServers?: RTCIceServer[]; videoRef?: React.MutableRefObject<HTMLVideoElement | null>; children?: ReactNode;
}) {
  const [fallback, setFallback] = useState(false);
  useEffect(() => { setFallback(false); }, [path]);
  if (!fallback) {
    return <WhepPlayer path={path} port={port} className={className} onUnsupported={onUnsupported} showSize={showSize}
      iceServers={iceServers} videoRef={videoRef} onFallback={() => setFallback(true)}>{children}</WhepPlayer>;
  }
  return <RecordingFollow camera={path.replace(/_sub$/, "")} className={className} videoRef={videoRef} onGiveUp={() => setFallback(false)}>{children}</RecordingFollow>;
}

/** Plays the recording from a few seconds ago and keeps loading the next chunk: live-ish without WebRTC. */
function RecordingFollow({ camera, className, videoRef, onGiveUp, children }: {
  camera: string; className?: string; videoRef?: React.MutableRefObject<HTMLVideoElement | null>; onGiveUp: () => void; children?: ReactNode;
}) {
  const ref = useRef<HTMLVideoElement | null>(null);
  const [start, setStart] = useState(() => nowS() - FALLBACK_LAG);
  const [errors, setErrors] = useState(0);
  const set = (el: HTMLVideoElement | null) => { ref.current = el; if (videoRef) videoRef.current = el; };
  useEffect(() => { if (errors >= 3) onGiveUp(); }, [errors, onGiveUp]);
  return (
    <div className={`player ${className ?? ""}`}>
      <video ref={set} key={start} src={playbackUrl(camera, start, CHUNK)} autoPlay muted playsInline
        onEnded={() => setStart((s) => Math.min(s + CHUNK, nowS() - FALLBACK_LAG))}
        onError={() => { setErrors((n) => n + 1); setTimeout(() => setStart(nowS() - FALLBACK_LAG), 3000); }} />
      <div className="player-size" title="WebRTC unavailable: playing the recording a few seconds behind live">recording · {FALLBACK_LAG}s behind</div>
      {children}
    </div>
  );
}
