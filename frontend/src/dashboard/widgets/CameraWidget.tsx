import { useEffect, useRef, useState } from "react";
import { LivePlayer } from "../../LivePlayer";
import { nowS } from "../../playback";
import { Icon } from "../../ui";
import { useBudget } from "../Dashboard";
import type { DashboardSource } from "../source";
import type { Widget } from "../types";

const iceCache = new Map<string, Promise<RTCIceServer[]>>();
export function useIceServers(source: DashboardSource, site: string): RTCIceServer[] | undefined {
  const [ice, setIce] = useState<RTCIceServer[] | undefined>(undefined);
  useEffect(() => {
    if (!site) return;
    if (!iceCache.has(site)) iceCache.set(site, source.iceServers(site).catch(() => []));
    let alive = true;
    iceCache.get(site)!.then((v) => { if (alive) setIce(v); });
    return () => { alive = false; };
  }, [source, site]);
  return ice;
}

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
  const [stillTs, setStillTs] = useState(() => nowS() - 3);

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
  useEffect(() => {
    if (playing) return;
    const t = setInterval(() => setStillTs(nowS() - 3), 10000);
    return () => clearInterval(t);
  }, [playing]);

  if (!site || !camera) return <div className="dash-empty muted small">Choose a camera in ⚙ settings.</div>;
  if (!cam) return <div className="dash-empty muted small">Camera {camera} is not on any site you can see.</div>;
  const hd = quality === "hd" && hdOk;
  const path = hd ? camera : `${camera}_sub`;
  const setQ = (q: "sd" | "hd") => { setQuality(q); if (editing) onProps({ quality: q }); };
  return (
    <div ref={box} className={`dash-cam ${big ? "big" : ""}`}>
      {playing && cam.online ? (
        <LivePlayer key={`${site}-${path}`} path={path} port={source.port} base={api!.base} iceServers={ice} className={hd ? "hd" : ""}
          onUnsupported={() => setHdOk(false)} />
      ) : (
        <div className="player dash-still">
          {cam.online ? <img src={api!.frameUrl(camera, stillTs, 640)} alt="" /> : <div className="player-state">{cam.siteName} is offline</div>}
          {cam.online && !editing && (
            <button className="dash-play" onClick={() => budget.force(w.id)} title="Play live (another tile pauses if the page is at its limit)"><Icon name="play" size={22} /></button>
          )}
        </div>
      )}
      <div className="dash-cam-bar">
        <span className={`dot ${cam.streamReady && cam.online ? "ok" : "bad"}`} />
        {!playing && cam.online && <span className="muted small">{editing ? "paused while editing" : "paused · still frame"}</span>}
        <span className="spacer" />
        <div className="segmented small-seg" title={hdOk ? "Stream quality" : "This browser can't decode the HD stream"}>
          <button className={!hd ? "active" : ""} onClick={() => setQ("sd")}>SD</button>
          <button className={hd ? "active" : ""} disabled={!hdOk} onClick={() => setQ("hd")}>HD</button>
        </div>
      </div>
    </div>
  );
}
