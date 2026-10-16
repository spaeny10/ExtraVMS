import { useEffect, useRef, useState } from "react";
import { NO_PICTURE_TEXT } from "../../liveFallback";
import { LivePlayer } from "../../LivePlayer";
import { Icon } from "../../ui";
import { useStill } from "../../useStill";
import { livePath } from "../../Views";
import { useBudget } from "../Dashboard";
import { useIceServers } from "../ice";
import type { DashboardSource } from "../source";
import type { Widget } from "../types";

// moved to ../ice so the hub's combined Live can share it; re-exported for existing importers
export { useIceServers };

export function cameraTitle(w: Widget<"camera">, source: DashboardSource): string {
  const cam = source.cameras().find((c) => c.site === w.props.site && c.id === w.props.camera);
  if (!cam) return w.props.camera ? `${w.props.camera} (unknown camera)` : "Camera";
  const sites = new Set(source.cameras().map((c) => c.site));
  return sites.size > 1 ? `${cam.siteName} · ${cam.name}` : cam.name;
}

/**
 * One live camera from any site. Streams count against the page budget (MAX_LIVE): tiles beyond it, or
 * scrolled out of view, show a still frame with a Play button instead of holding a WebRTC session.
 */
export function CameraWidget({ widget: w, source, editing, big, onProps }: {
  widget: Widget<"camera">; source: DashboardSource; editing: boolean; big: boolean; onProps: (p: Partial<Widget<"camera">["props"]>) => void;
}) {
  const { site, camera } = w.props;
  const cam = source.cameras().find((c) => c.site === site && c.id === camera);
  const api = site ? source.siteApi(site) : null;
  const ice = useIceServers(source, site);
  const budget = useBudget();
  const box = useRef<HTMLDivElement>(null);
  const [hdOk, setHdOk] = useState(true);
  const [quality, setQuality] = useState<"sd" | "hd">(w.props.quality ?? "sd");
  useEffect(() => { setQuality(w.props.quality ?? "sd"); }, [w.props.quality]);
  const [hasAudio, setHasAudio] = useState(false);
  const [sound, setSound] = useState(false);

  // visibility feeds the budget; offscreen tiles give their slot back
  const { visible } = budget;   // stable callback: the effect must not re-run when the budget's order changes
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    visible(w.id, true);   // assume on screen until the observer says otherwise (a hidden tab reports nothing until shown)
    const io = new IntersectionObserver(([e]) => visible(w.id, e.isIntersecting), { rootMargin: "100px" });
    io.observe(el);
    return () => { io.disconnect(); visible(w.id, false); };
  }, [visible, w.id]);
  const playing = (big || budget.playing(w.id)) && !editing;
  const still = useStill(!playing);

  if (!site || !camera) return <div className="dash-empty muted small">Choose a camera in ⚙ settings.</div>;
  if (!cam) return <div className="dash-empty muted small">Camera {camera} is not on any site you can see.</div>;
  const hd = quality === "hd" && hdOk;
  // SD is <id>_sub; HD is <id>, or <id>_hd on a camera that records its sub stream
  const path = livePath({ id: camera, record_stream: cam.recordStream }, hd);
  const setQ = (q: "sd" | "hd") => { setQuality(q); if (editing) onProps({ quality: q }); };
  return (
    <div ref={box} className={`dash-cam ${big ? "big" : ""}`}>
      {playing && cam.online ? (
        <LivePlayer key={`${site}-${path}`} path={path} port={source.port} base={api!.base} iceServers={ice} className={hd ? "hd" : ""}
          onUnsupported={hd ? () => setHdOk(false) : undefined} muted={!sound} onAudio={setHasAudio} />
      ) : (
        <div className="player dash-still">
          {cam.online
            ? <img src={api!.frameUrl(camera, still.ts, 640)} alt="" onLoad={still.onLoad} onError={still.onError} style={still.failed ? { visibility: "hidden" } : undefined} />
            : <div className="player-state">{cam.siteName} is offline</div>}
          {cam.online && still.failed && <div className="player-state">{NO_PICTURE_TEXT}</div>}
          {cam.online && !editing && (
            <button className="dash-play" onClick={() => budget.force(w.id)} title="Play live (another tile pauses if the page is at its limit)"><Icon name="play" size={22} /></button>
          )}
        </div>
      )}
      <div className="dash-cam-bar">
        <span className={`dot ${cam.streamReady && cam.online ? "ok" : "bad"}`} />
        {!playing && cam.online && <span className="muted small">{editing ? "paused while editing" : "paused · still frame"}</span>}
        <span className="spacer" />
        {playing && hasAudio && <button className={`ghost small ${sound ? "on" : ""}`} title={sound ? "Mute" : "Listen"} onClick={() => setSound((s) => !s)}>{sound ? "🔊" : "🔇"}</button>}
        <div className="segmented small-seg" title={hdOk ? "Stream quality" : "This browser can't decode the HD stream"}>
          <button className={!hd ? "active" : ""} onClick={() => setQ("sd")}>SD</button>
          <button className={hd ? "active" : ""} disabled={!hdOk} onClick={() => setQ("hd")}>HD</button>
        </div>
      </div>
    </div>
  );
}
