import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api, makeApi, whepSession, type SiteApi } from "./api";
import { cameraOfPath, stillWidth } from "./cameraStreams";
import { ZoomFrame } from "./VideoZoom";
import {
  type InboundVideo, type WhepStatsSample, type WhepTiming, type WhepTimingSnapshot, inboundVideo, pathQuality, reportLive, statsSample,
  whepRoute, whepTiming,
} from "./whepTiming";

/**
 * WebRTC (WHEP) player for a MediaMTX path.
 * onUnsupported fires when the browser can't decode the stream's codec (e.g. H.265 main streams),
 * so the caller can fall back to the H.264 sub stream.
 *
 * While it connects (until the first video frame plays) it shows the camera's latest recorded still (/api/frame,
 * sized to the player) with a small "Connecting…" badge, then cross-fades to the video. A still that can't load
 * leaves the plain black "Connecting…" box as before.
 *
 * Each connection attempt is timed (whepTiming.ts): onFirstFrame gets its stages when the first frame is presented
 * (requestVideoFrameCallback, else getStats polled until a frame decodes), and the registered live reporter, if any (the
 * hub's), gets a report then and when the attempt ends. onStats gets link quality every 2 s while connected.
 */
export type WhepState = "connecting" | "playing" | "error";

/** End a WHEP session on the server (DELETE its URL) so the camera's reader slot frees now, not when MediaMTX notices
 *  the peer is gone. keepalive: it still goes out when the page is closing. Some browsers refuse keepalive on a
 *  request that needs a CORS preflight (the direct-LAN client): then it is sent plainly. Failures don't matter. */
function endSession(url: string) {
  fetch(url, { method: "DELETE", keepalive: true })
    .catch(() => fetch(url, { method: "DELETE" }))
    .catch(() => {});
}

/** The camera's most recent still (from its recording) while the live picture connects. */
function usePoster(site: SiteApi, camera: string | null, box: React.RefObject<HTMLDivElement | null>) {
  const [url, setUrl] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [failed, setFailed] = useState(false);
  useLayoutEffect(() => {
    setLoaded(false);
    setFailed(false);
    if (!camera) { setUrl(null); return; }
    const w = box.current?.clientWidth ?? 0;
    // the newest keyframe on disk: the recording trails live by about a second (1 s fMP4 parts)
    setUrl(site.frameUrl(camera, Date.now() / 1000 - 1, stillWidth(w, window.devicePixelRatio || 1)));
  }, [site, camera, box]);
  return { url: failed ? null : url, loaded: loaded && !failed, onLoad: () => setLoaded(true), onError: () => setFailed(true) };
}

export function WhepPlayer({ path, port, className, onUnsupported, showSize = false, videoRef, children, iceServers, onFallback, base, site: given, muted = true, onAudio, onState, camera, poster = true, onFirstFrame, onStats }: {
  path: string; port: number; className?: string; onUnsupported?: () => void; showSize?: boolean;
  /** the attempt's first video frame was presented: how long each stage took (whepTiming.ts) */
  onFirstFrame?: (timing: WhepTimingSnapshot) => void;
  /** link quality every 2 s while connected (loss, freezes, dropped frames, frame size, jitter) */
  onStats?: (sample: WhepStatsSample) => void;
  /** the camera whose recording the still comes from; default: the path without _sub / _hd */
  camera?: string;
  /** show the camera's latest still while connecting (default on) */
  poster?: boolean;
  /** told whenever the connection state changes (the Timeline's live tiles report it as their status) */
  onState?: (s: WhepState) => void;
  /** sound off (default); unmuting must follow a click, browsers block autoplaying audio */
  muted?: boolean;
  /** told whether the stream carries an audio track the browser can play */
  onAudio?: (has: boolean) => void;
  /** URL prefix of the site that owns the camera ("/s/<site>" on the hub dashboard); default: this page's site */
  base?: string;
  /** the camera's server client; wins over `base` (the hub's direct client carries a token a bare base can't) */
  site?: SiteApi;
  /** STUN/TURN servers (a hub relay when viewed remotely); none = direct/LAN candidates only */
  iceServers?: RTCIceServer[];
  /** called after repeated connection failures so the caller can switch to a non-WebRTC picture */
  onFallback?: () => void;
  /** receives the <video> element (e.g. for an overlay that needs its real aspect ratio) */
  videoRef?: React.MutableRefObject<HTMLVideoElement | null>;
  /** overlays rendered inside the player box */
  children?: ReactNode;
}) {
  const video = useRef<HTMLVideoElement | null>(null);
  const setVideo = (el: HTMLVideoElement | null) => { video.current = el; if (videoRef) videoRef.current = el; };
  const [state, setStateRaw] = useState<WhepState>("connecting");
  const stateCb = useRef(onState);
  stateCb.current = onState;
  const setState = (s: WhepState) => { setStateRaw(s); stateCb.current?.(s); };
  const [size, setSize] = useState<string>("");
  const unsupported = useRef(onUnsupported);
  unsupported.current = onUnsupported;
  const fallback = useRef(onFallback);
  fallback.current = onFallback;
  const failures = useRef(0);
  const site = useMemo(() => given ?? (base === undefined ? api : makeApi(base)), [given, base]);
  const audioCb = useRef(onAudio);
  audioCb.current = onAudio;
  const firstFrameCb = useRef(onFirstFrame);
  firstFrameCb.current = onFirstFrame;
  const statsCb = useRef(onStats);
  statsCb.current = onStats;
  useEffect(() => { if (video.current) video.current.muted = muted; }, [muted]);  // React doesn't sync the muted attribute
  const box = useRef<HTMLDivElement | null>(null);
  const still = usePoster(site, poster ? camera ?? cameraOfPath(path) : null, box);
  // the first video frame of this path has played (a decoded frame with a size, not just the stream starting: its
  // audio track can arrive first): the still fades out, then is dropped
  const [frame, setFrame] = useState(false);
  const [stillGone, setStillGone] = useState(false);
  useEffect(() => { setFrame(false); setStillGone(false); }, [path]);
  const showStill = Boolean(still.url) && !stillGone && !(frame && !still.loaded);
  const stillVisible = showStill && still.loaded && !frame;

  useEffect(() => {
    let pc: RTCPeerConnection | null = null;
    let canceled = false;
    let retryTimer: number | undefined;
    let noFramesTimer: number | undefined;
    let sessionUrl: string | null = null;   // this connection's session on the server, from the answer's Location
    let attempt = 0;
    let timing: WhepTiming | null = null;   // the current attempt's marks; null once it has been reported as ended
    const route = whepRoute(site.whepUrl(path));   // hub, direct (the hub's LAN client) or local
    let framePoll: number | undefined;      // getStats polled for the first frame (no requestVideoFrameCallback)
    let frameReq: { v: HTMLVideoElement; id: number } | null = null;
    let statsTimer: number | undefined;
    const cam = camera ?? cameraOfPath(path);
    failures.current = 0;
    setSize("");
    const report = (t: WhepTiming, event: "first_frame" | "end") => reportLive({
      ...t.snapshot(), event, base: site.base, camera: cam, path, quality: pathQuality(path), route, duration_s: event === "end" ? t.elapsedS() : null,
    });
    const stopWatching = () => {
      clearInterval(framePoll);
      clearInterval(statsTimer);
      if (frameReq) frameReq.v.cancelVideoFrameCallback(frameReq.id);
      frameReq = null;
    };
    // the attempt is over (a retry, the fallback, unmount, the page closing): its "end" report, once
    const endAttempt = () => {
      stopWatching();
      if (timing) report(timing, "end");
      timing = null;
    };
    const hangUp = () => {
      endAttempt();
      pc?.close();
      if (sessionUrl) endSession(sessionUrl);
      sessionUrl = null;
    };
    const gotFrame = (t: WhepTiming) => {
      if (t !== timing || !t.mark("firstFrame")) return;
      clearInterval(framePoll);
      firstFrameCb.current?.(t.snapshot());
      report(t, "first_frame");
    };
    // first frame: the video's next presented frame, else (no requestVideoFrameCallback, or a background tab, where
    // nothing is presented) a decoded frame in getStats, polled every 250 ms for at most 15 s
    const watchFirstFrame = (t: WhepTiming, conn: RTCPeerConnection) => {
      const v = video.current;
      if (v && typeof v.requestVideoFrameCallback === "function" && document.visibilityState !== "hidden") {
        frameReq = { v, id: v.requestVideoFrameCallback(() => { frameReq = null; gotFrame(t); }) };
        return;
      }
      const until = performance.now() + 15000;
      framePoll = window.setInterval(async () => {
        if (performance.now() > until || t !== timing) { clearInterval(framePoll); return; }
        try {
          if (inboundVideo((await conn.getStats()).values())?.framesDecoded) gotFrame(t);
        } catch { /* closed meanwhile */ }
      }, 250);
    };
    // link quality for onStats, every 2 s while this connection is up
    const watchStats = (conn: RTCPeerConnection) => {
      clearInterval(statsTimer);
      let prev: InboundVideo | null = null;
      statsTimer = window.setInterval(async () => {
        if (!statsCb.current) return;
        try {
          const cur = inboundVideo((await conn.getStats()).values());
          if (!cur || canceled || conn !== pc) return;
          statsCb.current?.(statsSample(prev, cur));
          prev = { ...cur };
        } catch { /* closed meanwhile */ }
      }, 2000);
    };
    const onPageHide = () => endAttempt();   // effects don't clean up when the page goes away: report it now
    window.addEventListener("pagehide", onPageHide);

    const start = async () => {
      setState("connecting");
      const tm = (timing = whepTiming(++attempt));
      pc = new RTCPeerConnection(iceServers?.length ? { iceServers } : undefined);
      const conn = pc;
      watchFirstFrame(tm, conn);
      pc.addTransceiver("video", { direction: "recvonly" });
      pc.addTransceiver("audio", { direction: "recvonly" });   // answered only if the camera sends a WebRTC-playable codec
      audioCb.current?.(false);
      pc.ontrack = (ev) => {
        if (video.current) { video.current.srcObject = ev.streams[0]; video.current.muted = muted; }
        if (ev.track.kind === "audio") {
          // the answer always carries an audio m-line; the track stays "muted" unless the camera really sends sound
          const t = ev.track;
          const report = () => audioCb.current?.(!t.muted);
          t.onunmute = report; t.onmute = report;
          report();
        }
      };
      pc.onconnectionstatechange = () => {
        if (!pc) return;
        if (pc.connectionState === "connected") {
          setState("playing");
          tm.mark("connected");
          watchStats(conn);
          // Connected but nothing decodes: the codec negotiated but the decoder can't handle it.
          clearTimeout(noFramesTimer);
          noFramesTimer = window.setTimeout(async () => {
            const stats = pc ? [...(await pc.getStats()).values()] : [];
            const inbound = stats.find((s) => s.type === "inbound-rtp" && s.kind === "video") as { framesDecoded?: number } | undefined;
            if (!canceled && inbound && !inbound.framesDecoded) unsupported.current?.();
          }, 8000);
        }
        if (["failed", "disconnected"].includes(pc.connectionState)) retry();
      };
      try {
        const offer = await pc.createOffer();
        await pc.setLocalDescription(offer);
        tm.mark("offer");
        await new Promise<void>((resolve) => {
          if (pc!.iceGatheringState === "complete") return resolve();
          // relay candidates take longer
          const t = setTimeout(() => { tm.iceTimedOut(); resolve(); }, iceServers?.length ? 3000 : 1500);
          pc!.onicegatheringstatechange = () => {
            if (pc!.iceGatheringState === "complete") {
              clearTimeout(t);
              resolve();
            }
          };
        });
        tm.mark("iceGathered");
        // same-origin signaling via the NVR (works over HTTPS); media flows directly from MediaMTX
        const r = await fetch(site.whepUrl(path), {
          method: "POST",
          headers: { "Content-Type": "application/sdp" },
          body: pc.localDescription!.sdp,
        });
        tm.mark("posted");
        if (!r.ok) {
          const text = await r.text();
          // MediaMTX rejects offers that lack the track's codec ("codecs not supported by client").
          if (r.status === 400 && /codec/i.test(text) && unsupported.current) {
            canceled = true;
            unsupported.current();
            return;
          }
          throw new Error(`WHEP ${r.status}`);
        }
        const session = whepSession(r.headers.get("location"), path);
        sessionUrl = session ? site.whepSessionUrl(path, session) : null;
        if (canceled) { hangUp(); return; }
        await pc.setRemoteDescription({ type: "answer", sdp: await r.text() });
        tm.mark("answer");
      } catch {
        retry();
      }
    };

    const retry = () => {
      if (canceled) return;
      setState("error");
      if (++failures.current >= 2 && fallback.current) { canceled = true; hangUp(); fallback.current(); return; }
      hangUp();
      pc = null;
      clearTimeout(retryTimer);
      retryTimer = window.setTimeout(start, 4000);
    };

    start();
    return () => {
      canceled = true;
      window.removeEventListener("pagehide", onPageHide);
      clearTimeout(retryTimer);
      clearTimeout(noFramesTimer);
      hangUp();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, port, site, JSON.stringify(iceServers ?? [])]);

  return (
    <div ref={box} className={`player ${className ?? ""}`}>
      <ZoomFrame>
        <video
          ref={setVideo}
          autoPlay
          muted
          playsInline
          onPlaying={(e) => {
            // frames decode: a later drop gets its retry again before the caller's fallback
            if (e.currentTarget.videoWidth) { setFrame(true); failures.current = 0; }
          }}
          onResize={(e) => {
            const v = e.currentTarget;
            if (v.videoWidth) setSize(`${v.videoWidth}×${v.videoHeight}`);
            if (v.videoWidth && !v.paused) setFrame(true);
          }}
        />
      </ZoomFrame>
      {/* the still shows only until the first frame, while the picture is still at 1x: it needs no zoom layer */}
      {showStill && (
        <img className={`player-poster ${stillVisible ? "shown" : ""}`} src={still.url!} alt="" draggable={false}
          onLoad={still.onLoad} onError={still.onError} onTransitionEnd={() => { if (frame) setStillGone(true); }} />
      )}
      {stillVisible
        ? <div className="player-badge" role="status">{state === "error" ? "Reconnecting…" : "Connecting…"}</div>
        : state !== "playing" && <div className="player-state">{state === "connecting" ? "Connecting…" : "Reconnecting…"}</div>}
      {showSize && size && state === "playing" && <div className="player-size">{size}</div>}
      {children}
    </div>
  );
}
