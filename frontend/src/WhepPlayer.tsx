import { useEffect, useRef, useState } from "react";

/**
 * WebRTC (WHEP) player for a MediaMTX path.
 * onUnsupported fires when the browser can't decode the stream's codec (e.g. H.265 main streams),
 * so the caller can fall back to the H.264 sub stream.
 */
export function WhepPlayer({ path, port, className, onUnsupported, showSize = false }: {
  path: string; port: number; className?: string; onUnsupported?: () => void; showSize?: boolean;
}) {
  const video = useRef<HTMLVideoElement>(null);
  const [state, setState] = useState<"connecting" | "playing" | "error">("connecting");
  const [size, setSize] = useState<string>("");
  const unsupported = useRef(onUnsupported);
  unsupported.current = onUnsupported;

  useEffect(() => {
    let pc: RTCPeerConnection | null = null;
    let cancelled = false;
    let retryTimer: number | undefined;
    let noFramesTimer: number | undefined;
    setSize("");

    const start = async () => {
      setState("connecting");
      pc = new RTCPeerConnection();
      pc.addTransceiver("video", { direction: "recvonly" });
      pc.ontrack = (ev) => {
        if (video.current) video.current.srcObject = ev.streams[0];
      };
      pc.onconnectionstatechange = () => {
        if (!pc) return;
        if (pc.connectionState === "connected") {
          setState("playing");
          // Connected but nothing decodes: the codec negotiated but the decoder can't handle it.
          clearTimeout(noFramesTimer);
          noFramesTimer = window.setTimeout(async () => {
            const stats = pc ? [...(await pc.getStats()).values()] : [];
            const inbound = stats.find((s) => s.type === "inbound-rtp" && s.kind === "video") as { framesDecoded?: number } | undefined;
            if (!cancelled && inbound && !inbound.framesDecoded) unsupported.current?.();
          }, 8000);
        }
        if (["failed", "disconnected"].includes(pc.connectionState)) retry();
      };
      try {
        const offer = await pc.createOffer();
        await pc.setLocalDescription(offer);
        await new Promise<void>((resolve) => {
          if (pc!.iceGatheringState === "complete") return resolve();
          const t = setTimeout(resolve, 1500);
          pc!.onicegatheringstatechange = () => {
            if (pc!.iceGatheringState === "complete") {
              clearTimeout(t);
              resolve();
            }
          };
        });
        // same-origin signalling via the NVR (works over HTTPS); media flows directly from MediaMTX
        const r = await fetch(`/api/whep/${path}`, {
          method: "POST",
          headers: { "Content-Type": "application/sdp" },
          body: pc.localDescription!.sdp,
        });
        if (!r.ok) {
          const text = await r.text();
          // MediaMTX rejects offers that lack the track's codec ("codecs not supported by client").
          if (r.status === 400 && /codec/i.test(text) && unsupported.current) {
            cancelled = true;
            unsupported.current();
            return;
          }
          throw new Error(`WHEP ${r.status}`);
        }
        if (cancelled) return;
        await pc.setRemoteDescription({ type: "answer", sdp: await r.text() });
      } catch {
        retry();
      }
    };

    const retry = () => {
      if (cancelled) return;
      setState("error");
      pc?.close();
      pc = null;
      clearTimeout(retryTimer);
      retryTimer = window.setTimeout(start, 4000);
    };

    start();
    return () => {
      cancelled = true;
      clearTimeout(retryTimer);
      clearTimeout(noFramesTimer);
      pc?.close();
    };
  }, [path, port]);

  return (
    <div className={`player ${className ?? ""}`}>
      <video
        ref={video}
        autoPlay
        muted
        playsInline
        onResize={(e) => {
          const v = e.currentTarget;
          if (v.videoWidth) setSize(`${v.videoWidth}×${v.videoHeight}`);
        }}
      />
      {state !== "playing" && <div className="player-state">{state === "connecting" ? "Connecting…" : "Reconnecting…"}</div>}
      {showSize && size && state === "playing" && <div className="player-size">{size}</div>}
    </div>
  );
}
