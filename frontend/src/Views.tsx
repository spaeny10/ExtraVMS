import { useEffect, useState } from "react";
import { BASE, api, fmtTime, frameUrl, type BaselineCamera, type FootageStatus, type RemoteStatus, type Camera, type FeedbackStats, type HubStatus, type NvrEvent, type RetentionPolicy, type SiteApi, type SystemInfo, type Zone } from "./api";
import { EventCard } from "./Events";
import { EventDetail } from "./EventDetail";
import { LivePlayer } from "./LivePlayer";
import { ZoneEditor } from "./ZoneEditor";
import { PolicyForm, RetentionPanel } from "./RetentionPanel";
import { Advisor } from "./Advisor";
import { QwenFeedbackInfo } from "./QwenFeedbackInfo";
import { NeighborsEditor } from "./Neighbors";
import { Skeleton, confirmDialog, errorText, swipeHandlers, toast, useIsPhone } from "./ui";
import { RegionBadge, RegionOverlay } from "./RegionPaint";
import { PtzBadge, PtzOverlay, PtzSettings } from "./PtzControl";
import { regionPass, regions, useRegions } from "./region";
import { useRef } from "react";
import { yoloFallbackText, yoloState } from "./yoloStatus";

/* ------------------------------------------------------------------ Live */

type Quality = "sd" | "hd";

function loadQuality(): Record<string, Quality> {
  try {
    return JSON.parse(localStorage.getItem("liveQuality") ?? "{}");
  } catch {
    return {};
  }
}

/** One live camera: the WHEP player with the paint-a-region overlay and the tile bar. */
export function LiveTile({ c, hd, port, active, onUnsupported, bar, phone, onSwipe, iceServers, site = api, regionKey }: {
  c: Camera; hd: boolean; port: number; active?: NvrEvent; onUnsupported?: () => void; bar: React.ReactNode; phone?: boolean; onSwipe?: (dir: -1 | 1) => void;
  iceServers?: RTCIceServer[];
  /** the camera's server (the hub's combined Live); default: this server */
  site?: SiteApi;
  /** key the painted region is stored under (the hub uses server/camera); default: the camera id */
  regionKey?: string;
}) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const [painting, setPainting] = useState(false);
  const [ptzOn, setPtzOn] = useState(false);
  const [hasAudio, setHasAudio] = useState(false);
  const [sound, setSound] = useState(false);
  const ptz = c.status?.ptz;
  const aspect = hd ? 2592 / 1520 : 4 / 3;
  return (
    <div className={`tile ${active ? "alerting" : ""} ${painting ? "painting" : ""} ${ptzOn ? "ptz" : ""}`}
      {...(phone && onSwipe && !painting && !ptzOn ? swipeHandlers(onSwipe) : {})}>
      <LivePlayer key={`${c.id}-${hd ? "hd" : "sd"}`} path={hd ? c.id : `${c.id}_sub`} port={port} showSize className={hd ? "hd" : ""}
        site={site === api ? undefined : site}
        onUnsupported={onUnsupported} videoRef={videoRef} iceServers={iceServers} muted={!sound} onAudio={setHasAudio}>
        {!ptzOn && <RegionOverlay cam={regionKey ?? c.id} videoRef={videoRef} editing={painting} onDone={() => setPainting(false)} camera={c} fallbackAspect={aspect} site={site} />}
        {ptz?.available && <PtzOverlay cam={c.id} videoRef={videoRef} active={ptzOn} onDone={() => setPtzOn(false)} fallbackAspect={aspect} site={site} />}
      </LivePlayer>
      <div className="tile-bar">
        {bar}
        <PtzBadge cam={c.id} ptz={ptz} site={site} />
        {!painting && <RegionBadge cam={regionKey ?? c.id} onEdit={() => setPainting(true)} />}
        {hasAudio && <button className={`ghost small ${sound ? "on" : ""}`} title={sound ? "Mute" : "Listen"} onClick={() => setSound((s) => !s)}>{sound ? "🔊" : "🔇"}</button>}
        {ptz?.available && (
          <button className={`ghost small ${ptzOn ? "on" : ""}`} title={ptz.pan_tilt === false ? "Zoom, relay, digital input" : "Pan / tilt / zoom, presets, relay"} disabled={painting}
            onClick={() => setPtzOn((p) => !p)}>🕹</button>
        )}
        <button className={`ghost small ${painting ? "on" : ""}`} title="Paint a region: the activity feed shows only events that passed through it" disabled={ptzOn}
          onClick={() => setPainting((p) => !p)}>✎</button>
      </div>
    </div>
  );
}

/** The activity feed filtered by painted regions, with a note and a way to clear them. */
function RegionNote({ cameras }: { cameras: Camera[] }) {
  const regionMap = useRegions();
  const ids = Object.keys(regionMap);
  if (!ids.length) return null;
  return (
    <p className="muted small">
      Showing only {ids.map((id) => cameras.find((c) => c.id === id)?.name ?? id).join(" and ")} events that passed through the painted region ·{" "}
      <button className="linkish" onClick={() => ids.forEach((id) => regions.set(id, null))}>Clear</button>
    </p>
  );
}

export function LiveView({ cameras, port, recent }: { cameras: Camera[]; port: number; recent: NvrEvent[] }) {
  const [focus, setFocus] = useState<string | null>(null);
  // ICE servers: a TURN relay when this UI is served through the fleet hub (or the site knows the hub's relay)
  const [iceServers, setIceServers] = useState<RTCIceServer[] | undefined>(undefined);
  useEffect(() => { api.turn().then((t) => setIceServers(t.iceServers)).catch(() => setIceServers([])); }, []);
  const regionMap = useRegions();
  // a painted region scopes the feed to that camera (or cameras): only their events, only through the region.
  // `recent` is just the last few events site-wide, so fetch a deeper history for the scoped cameras.
  const regionKey = Object.keys(regionMap).sort().join(",");
  const scoped = regionKey.length > 0;
  const [scopedEvents, setScopedEvents] = useState<NvrEvent[]>([]);
  useEffect(() => {
    if (!scoped) { setScopedEvents([]); return; }
    let cancelled = false;
    Promise.all(regionKey.split(",").map((camera) => api.events({ camera, status: "open,pending,verified", limit: 100 }).catch(() => [] as NvrEvent[])))
      .then((lists) => { if (!cancelled) setScopedEvents(lists.flat()); });
    return () => { cancelled = true; };
  }, [regionKey, scoped]);
  const pool = scoped
    ? [...new Map([...scopedEvents, ...recent].map((e) => [e.id, e])).values()].sort((a, b) => b.start_ts - a.start_ts)
    : recent;
  const feed = pool.filter((e) => !scoped || (regionMap[e.camera_id] && regionPass(e, regionMap[e.camera_id])));
  const [open, setOpen] = useState<number | null>(null);
  // SD = H.264 sub stream (light, plays everywhere); HD = the recorded H.265 main stream.
  const [quality, setQualityState] = useState<Record<string, Quality>>(loadQuality);
  const [hdUnsupported, setHdUnsupported] = useState(false);
  const setQuality = (next: Record<string, Quality>) => {
    setQualityState(next);
    try {
      localStorage.setItem("liveQuality", JSON.stringify(next));
    } catch {
      /* private mode */
    }
  };
  const q = (id: string): Quality => (hdUnsupported ? "sd" : quality[id] ?? "sd");
  const setAll = (v: Quality) => setQuality(Object.fromEntries(cameras.map((c) => [c.id, v])));
  const allHd = cameras.length > 0 && cameras.every((c) => q(c.id) === "hd");
  const allSd = cameras.every((c) => q(c.id) === "sd");
  const name = (id: string) => cameras.find((c) => c.id === id)?.name ?? id;
  const shown = focus ? cameras.filter((c) => c.id === focus) : cameras;
  const cols = focus ? 1 : Math.min(4, Math.ceil(Math.sqrt(Math.max(1, cameras.length))));
  const isPhone = useIsPhone();
  const [phoneCam, setPhoneCam] = useState(0);
  const [stripTick, setStripTick] = useState(0);
  useEffect(() => {
    if (!isPhone) return;
    const t = setInterval(() => setStripTick((x) => x + 1), 10000); // refresh the strip stills
    return () => clearInterval(t);
  }, [isPhone]);
  if (isPhone && cameras.length > 0) {
    const idx = Math.min(phoneCam, cameras.length - 1);
    const c = cameras[idx];
    const hd = q(c.id) === "hd";
    const active = recent.find((e) => e.camera_id === c.id && (e.status === "open" || e.status === "pending"));
    const go = (dir: -1 | 1) => setPhoneCam((i) => (i + dir + cameras.length) % cameras.length);
    return (
      <div className="live-phone">
        <LiveTile c={c} hd={hd} port={port} active={active} phone onSwipe={go} iceServers={iceServers} onUnsupported={hd ? () => setHdUnsupported(true) : undefined} bar={<>
          <span className={`dot ${c.status?.stream_ready ? "ok" : "bad"}`} />
          <span>{c.name}</span>
          {active && <span className={`label-chip ${active.camera_class}`}>{active.camera_class}</span>}
          <span className="spacer" />
          <div className="segmented small-seg">
            <button className={!hd ? "active" : ""} onClick={() => setQuality({ ...quality, [c.id]: "sd" })}>SD</button>
            <button className={hd ? "active" : ""} disabled={hdUnsupported} onClick={() => setQuality({ ...quality, [c.id]: "hd" })}>HD</button>
          </div>
        </>} />
        {cameras.length > 1 && (
          <div className="live-strip" role="tablist">
            {cameras.map((x, i) => (
              <button key={x.id} role="tab" aria-selected={i === idx} className={`live-strip-item ${i === idx ? "active" : ""}`} onClick={() => setPhoneCam(i)}>
                <img src={`${frameUrl(x.id, Date.now() / 1000 - 15, 320)}&r=${stripTick}`} alt="" />
                <span><span className={`dot ${x.status?.stream_ready ? "ok" : "bad"}`} /> {x.name}</span>
              </button>
            ))}
          </div>
        )}
        <p className="muted small center">Swipe the picture or tap a thumbnail to switch cameras.</p>
        <aside className="live-feed">
          <h3>Latest activity</h3>
          <RegionNote cameras={cameras} />
          {recent.length === 0 && <p className="muted">Nothing yet.</p>}
          {recent.length > 0 && feed.length === 0 && <p className="muted">Nothing recent on that camera passed through the painted region.</p>}
          {feed.slice(0, 12).map((e) => <EventCard key={e.id} e={e} cameraName={name(e.camera_id)} onOpen={() => setOpen(e.id)} />)}
        </aside>
        {open !== null && <EventDetail id={open} cameraName={name} onClose={() => setOpen(null)} />}
      </div>
    );
  }
  return (
    <div className="live-layout">
      <div className="live-main">
        <div className="toolbar live-toolbar">
          <span className="muted small">Stream</span>
          <div className="segmented">
            <button className={allSd ? "active" : ""} onClick={() => setAll("sd")} title="H.264 sub stream: low bandwidth, plays in any browser">All SD</button>
            <button className={allHd ? "active" : ""} disabled={hdUnsupported} onClick={() => setAll("hd")} title="Full-resolution H.265 main stream">All HD</button>
          </div>
          {hdUnsupported && <span className="muted small">This browser can't play the H.265 main stream, so live view is using SD. Chrome or Edge on Windows with a GPU can play it.</span>}
        </div>
        <div className="live-grid" style={{ gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))` }}>
          {shown.map((c) => {
            const active = recent.find((e) => e.camera_id === c.id && (e.status === "open" || e.status === "pending"));
            const hd = q(c.id) === "hd";
            return (
              <LiveTile key={c.id} c={c} hd={hd} port={port} active={active} iceServers={iceServers} onUnsupported={hd ? () => setHdUnsupported(true) : undefined} bar={<>
                <span className={`dot ${c.status?.stream_ready ? "ok" : "bad"}`} title={c.status?.stream_ready ? "Recording" : "Offline"} />
                <span>{c.name}</span>
                {active && <span className={`label-chip ${active.camera_class}`}>{active.camera_class}</span>}
                <span className="spacer" />
                <div className="segmented small-seg" title="Stream quality for this camera">
                  <button className={!hd ? "active" : ""} onClick={() => setQuality({ ...quality, [c.id]: "sd" })}>SD</button>
                  <button className={hd ? "active" : ""} disabled={hdUnsupported} onClick={() => setQuality({ ...quality, [c.id]: "hd" })}>HD</button>
                </div>
                <button className="ghost small" onClick={() => setFocus(focus ? null : c.id)}>{focus ? "Grid" : "Expand"}</button>
              </>} />
            );
          })}
          {cameras.length === 0 && <div className="empty">No cameras yet. Add one under Cameras.</div>}
        </div>
      </div>
      <aside className="live-feed">
        <h3>Latest activity</h3>
        <RegionNote cameras={cameras} />
        {recent.length === 0 && <p className="muted">Nothing yet.</p>}
        {recent.length > 0 && feed.length === 0 && <p className="muted">Nothing recent on that camera passed through the painted region.</p>}
        {feed.slice(0, 12).map((e) => (
          <EventCard key={e.id} e={e} cameraName={name(e.camera_id)} onOpen={() => setOpen(e.id)} />
        ))}
      </aside>
      {open !== null && <EventDetail id={open} cameraName={name} onClose={() => setOpen(null)} />}
    </div>
  );
}

/* ------------------------------------------------------------------ Cameras */

const blank: Camera & { password?: string } = {
  id: "", name: "", host: "", onvif_port: 80, rtsp_port: 554, username: "admin", main_path: "/main",
  sub_path: "/sub", enabled: true, zones: [], retention_days: null, scene_notes: "",
};

export function CamerasView({ cameras, port, reload }: { cameras: Camera[]; port: number; reload: () => void }) {
  const [edit, setEdit] = useState<(Camera & { password?: string }) | null>(null);
  const [err, setErr] = useState("");
  const [zoneCam, setZoneCam] = useState<Camera | null>(null);
  const [sitePolicy, setSitePolicy] = useState<RetentionPolicy | null>(null);
  useEffect(() => {
    if (edit && !sitePolicy) api.retentionPolicy().then((r) => setSitePolicy(r.policy)).catch(() => {});
  }, [edit, sitePolicy]);
  const save = async () => {
    if (!edit) return;
    setErr("");
    try {
      await api.saveCamera({ ...edit, password: edit.password || undefined });
      setEdit(null);
      reload();
      toast.success(`Camera "${edit.name}" saved`);
    } catch (e) {
      setErr(errorText(e));
      toast.error(e);
    }
  };
  return (
    <div className="view">
      <div className="toolbar">
        <button onClick={() => setEdit({ ...blank, id: `cam${cameras.length + 1}` })}>Add camera</button>
      </div>
      <table className="table">
        <thead>
          <tr><th>Camera</th><th>Address</th><th>Stream</th><th>Bitrate</th><th>Metadata</th><th>ONVIF events</th><th>PTZ</th><th>Zones</th><th>Retention</th><th /></tr>
        </thead>
        <tbody>
          {cameras.map((c) => (
            <tr key={c.id} className={c.enabled ? "" : "muted"}>
              <td><strong>{c.name}</strong> <span className="muted small">{c.id}</span></td>
              <td>{c.host}</td>
              <td><Health ok={c.status?.stream_ready} /> {c.status?.tracks?.join(", ")}
                {c.status?.health?.problems?.length ? <div className="small error">{c.status.health.problems.join("; ")}</div> : null}</td>
              <td title="Main stream now · estimated recording per day (from the last hour)">
                {c.status?.health?.bitrate_mbps != null ? `${c.status.health.bitrate_mbps.toFixed(1)} Mbps` : "—"}
                {c.status?.health?.gb_per_day != null && <div className="muted small">~{c.status.health.gb_per_day} GB/day</div>}
              </td>
              <td><Health ok={c.status?.metadata} /></td>
              <td><Health ok={c.status?.onvif_events} /></td>
              <td>{!c.status?.ptz?.available ? <span className="muted">—</span>
                : c.status.ptz.last_error ? <span className="error small">unreachable</span>
                : c.status.ptz.at_home ? "home"
                : c.status.ptz.home_name ? <span className="ptz-badge">↗ {c.status.ptz.preset_name ?? "away"}</span>
                : <span className="muted small">no home set</span>}
                {c.status?.ptz?.relay?.state ? <span className="ptz-badge"> ⚡</span> : null}</td>
              <td>{zoneSummary(c.zones)}</td>
              <td>{c.retention_days ?? "default"}</td>
              <td className="row">
                <button className="ghost small" onClick={() => setZoneCam(c)}>Zones</button>
                <button className="ghost small" onClick={() => setEdit({ ...c })}>Edit</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {edit && (
        <div className="modal-backdrop" onClick={() => setEdit(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <header className="modal-head">
              <h2>{cameras.some((c) => c.id === edit.id) ? `Edit ${edit.name}` : "Add camera"}</h2>
              <button className="ghost" onClick={() => setEdit(null)} aria-label="Close">✕</button>
            </header>
            <div className="modal-grid">
              <div className="form">
                <Field label="ID"><input value={edit.id} disabled={cameras.some((c) => c.id === edit.id)} onChange={(e) => setEdit({ ...edit, id: e.target.value })} /></Field>
                <Field label="Name"><input value={edit.name} onChange={(e) => setEdit({ ...edit, name: e.target.value })} /></Field>
                <Field label="Host / IP"><input value={edit.host} onChange={(e) => setEdit({ ...edit, host: e.target.value })} /></Field>
                <div className="row">
                  <Field label="ONVIF port"><input type="number" value={edit.onvif_port} onChange={(e) => setEdit({ ...edit, onvif_port: +e.target.value })} /></Field>
                  <Field label="RTSP port"><input type="number" value={edit.rtsp_port} onChange={(e) => setEdit({ ...edit, rtsp_port: +e.target.value })} /></Field>
                </div>
                <div className="row">
                  <Field label="Username"><input value={edit.username} onChange={(e) => setEdit({ ...edit, username: e.target.value })} /></Field>
                  <Field label="Password"><input type="password" placeholder="unchanged" autoComplete="new-password" value={edit.password ?? ""} onChange={(e) => setEdit({ ...edit, password: e.target.value })} /></Field>
                </div>
                <div className="row">
                  <Field label="Main stream path"><input value={edit.main_path} onChange={(e) => setEdit({ ...edit, main_path: e.target.value })} /></Field>
                  <Field label="Sub stream path"><input value={edit.sub_path} onChange={(e) => setEdit({ ...edit, sub_path: e.target.value })} /></Field>
                </div>
                <div className="row">
                  <Field label="Enabled"><input type="checkbox" checked={Boolean(edit.enabled)} onChange={(e) => setEdit({ ...edit, enabled: e.target.checked })} /></Field>
                </div>
                <div className="field">
                  <span>Qwen describes</span>
                  <div className="row">
                    {(["person", "vehicle"] as const).map((l) => {
                      const cur = edit.synopsis_labels ?? ["person"];
                      return (
                        <label key={l} className="row small">
                          <input type="checkbox" checked={cur.includes(l)}
                            onChange={(e) => setEdit({ ...edit, synopsis_labels: e.target.checked ? [...cur.filter((x) => x !== l), l] : cur.filter((x) => x !== l) })} />
                          {l === "person" ? "People" : "Vehicles"}
                        </label>
                      );
                    })}
                  </div>
                  <span className="small">YOLO-verified detections only, and only inside this camera's zones. Unusual events are described either way.</span>
                </div>
                <div className="field">
                  <span>Site rules</span>
                  {(edit.policies ?? []).map((r, i) => (
                    <div key={i} className="rule-row">
                      <span className="small">Only</span>
                      <input value={r.allowed.join(", ")} placeholder={r.kind === "entry" ? "names from People & vehicles, e.g. Shawn" : "names from People & vehicles, e.g. BIGView truck"}
                        onChange={(e) => setEdit({ ...edit, policies: (edit.policies ?? []).map((x, k) => k === i ? { ...x, allowed: e.target.value.split(",").map((s) => s.trim()).filter(Boolean) } : x) })} />
                      {r.kind === "entry" ? (
                        <>
                          <span className="small">may enter through</span>
                          <select value={r.area ?? ""} onChange={(e) => setEdit({ ...edit, policies: (edit.policies ?? []).map((x, k) => k === i ? { ...x, area: e.target.value } : x) })}>
                            <option value="">choose a named place…</option>
                            {(edit.zones ?? []).filter((z) => z.type === "area").map((z) => <option key={z.name} value={z.name}>{z.name}</option>)}
                          </select>
                        </>
                      ) : (
                        <>
                          <span className="small">may tow a</span>
                          <input value={r.asset ?? ""} placeholder="solar light tower"
                            onChange={(e) => setEdit({ ...edit, policies: (edit.policies ?? []).map((x, k) => k === i ? { ...x, asset: e.target.value } : x) })} />
                        </>
                      )}
                      <select value={r.priority} onChange={(e) => setEdit({ ...edit, policies: (edit.policies ?? []).map((x, k) => k === i ? { ...x, priority: e.target.value as "medium" | "high" } : x) })}>
                        <option value="high">high priority</option><option value="medium">medium priority</option>
                      </select>
                      <button className="ghost small" title="Remove rule" onClick={() => setEdit({ ...edit, policies: (edit.policies ?? []).filter((_, k) => k !== i) })}>✕</button>
                    </div>
                  ))}
                  <div className="row">
                    <button className="ghost small" onClick={() => setEdit({ ...edit, policies: [...(edit.policies ?? []), { kind: "towing", asset: "", allowed: [], priority: "high" }] })}>+ Towing rule</button>
                    <button className="ghost small" onClick={() => setEdit({ ...edit, policies: [...(edit.policies ?? []), { kind: "entry", area: "", allowed: [], priority: "high" }] })}>+ Entry rule</button>
                  </div>
                  <span className="small">Towing: checked after Qwen describes each vehicle here — a vehicle towing the asset that isn't a named vehicle is flagged. Entry: checked right after verification — a person whose track starts at that named place (an exterior door) who isn't a named person is flagged. Both raise the event to that priority and show it under Needs attention. Name your own people and trucks in People & vehicles first; an unnamed one trips the rule once.</span>
                </div>
                <label className="field">
                  <span>Scene notes for Qwen</span>
                  <textarea rows={5} value={edit.scene_notes ?? ""} onChange={(e) => setEdit({ ...edit, scene_notes: e.target.value })}
                    placeholder={"What's normal in this view, e.g.\nThe white trailers in the foreground are our solar light towers.\nThe road and Ford dealership in the background are public; traffic there is routine.\nStaff wear hi-vis vests."} />
                  <span className="small">Added to every synopsis and chat prompt for this camera.</span>
                </label>
                {err && <p className="error">{err}</p>}
                <div className="row"><button onClick={save}>Save</button></div>
              </div>
              <div>
                <h3>Detection zones</h3>
                <p>{zoneSummary(edit.zones)}</p>
                <p className="muted small">Mask out busy areas such as a road, or limit detection to the areas you care about. Drawn on a full-resolution still with recent detections shown.</p>
                {cameras.some((c) => c.id === edit.id) ? (
                  <button className="ghost" onClick={() => { const c = cameras.find((x) => x.id === edit.id)!; setEdit(null); setZoneCam(c); }}>Edit zones…</button>
                ) : (
                  <p className="muted small">Save the camera first.</p>
                )}
                <h3 className="spaced">Neighbouring cameras</h3>
                {cameras.some((c) => c.id === edit.id) ? <NeighborsEditor cameraId={edit.id} cameras={cameras} /> : <p className="muted small">Save the camera first.</p>}
                <h3 className="spaced">PTZ</h3>
                {cameras.some((c) => c.id === edit.id) ? <PtzSettings camera={cameras.find((c) => c.id === edit.id)!} onChanged={reload} /> : <p className="muted small">Save the camera first.</p>}
                <h3 className="spaced">Retention</h3>
                <label className="row small">
                  <input type="checkbox" checked={!edit.retention_policy} onChange={(e) => setEdit({
                    ...edit, retention_days: null,
                    retention_policy: e.target.checked ? null : { ...(sitePolicy ?? {}), ...(edit.retention_days ? { continuous_days: edit.retention_days } : {}) },
                  })} />
                  Use the site retention policy{sitePolicy ? ` (${sitePolicy.continuous_days} days continuous)` : ""}
                </label>
                {edit.retention_policy && sitePolicy && (
                  <PolicyForm value={{ ...sitePolicy, ...edit.retention_policy, keep: { ...sitePolicy.keep, ...(edit.retention_policy.keep ?? {}) } } as RetentionPolicy}
                    showFloor={false} onChange={(v) => { const { min_free_gb: _floor, ...rest } = v; setEdit({ ...edit, retention_policy: rest }); }} />
                )}
              </div>
            </div>
          </div>
        </div>
      )}
      {zoneCam && <ZoneEditor camera={zoneCam} onClose={() => setZoneCam(null)} onSaved={reload} />}
    </div>
  );
}

function zoneSummary(zones: Zone[]): string {
  const valid = zones.filter((z) => z.points.length >= 3);
  if (!valid.length) return "Full frame";
  const count = (t: string) => valid.filter((z) => (z.type ?? "include") === t).length;
  const masks = count("exclude"), areas = count("include"), places = count("area"), ppe = count("ppe");
  return [masks && `${masks} mask${masks > 1 ? "s" : ""}`, areas && `${areas} detect-only area${areas > 1 ? "s" : ""}`,
    places && `${places} place${places > 1 ? "s" : ""}`, ppe && `${ppe} PPE zone${ppe > 1 ? "s" : ""}`].filter(Boolean).join(", ");
}

function Health({ ok }: { ok?: boolean }) {
  return <span className={`dot ${ok ? "ok" : "bad"}`} />;
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="field">
      <span>{label}</span>
      {children}
    </label>
  );
}

/* ------------------------------------------------------------------ System */

/** One line of a settings group: label, value (+ small note), optional action on the right. */
function Row({ label, hint, value, sub, action, children }: {
  label: string; hint?: string; value?: React.ReactNode; sub?: React.ReactNode; action?: React.ReactNode; children?: React.ReactNode;
}) {
  return (
    <div className="sys-row">
      <div className="sys-label" title={hint}>{label}{hint && <span className="muted"> ⓘ</span>}</div>
      <div className="sys-value">
        {value}
        {sub && <div className="muted small">{sub}</div>}
        {children}
      </div>
      {action && <div className="sys-action">{action}</div>}
    </div>
  );
}

/** Settings -> System: this site's link to the fleet hub (claim code while unenrolled). */
function HubPanel() {
  const [h, setH] = useState<HubStatus | null>(null);
  const [url, setUrl] = useState<string | null>(null);
  const [urlErr, setUrlErr] = useState<string | null>(null);
  const load = () => api.hub().then(setH).catch(() => {});
  // A different hub address un-enrols this server (its device token belongs to the old hub), so it shows a new
  // claim code and must be claimed again at the new hub.
  const saveUrl = async () => {
    if (h?.enrolled && !(await confirmDialog("Changing the hub URL un-enrols this server; it must be claimed again at the new hub.", { confirmLabel: "Change and un-enrol", danger: true }))) return;
    try {
      await api.setHub({ hub_url: url! });
      setUrl(null);
      setUrlErr(null);
    } catch (e) {
      setUrlErr(String(e instanceof Error ? e.message : e).replace(/^\d+ /, ""));
    }
    load();
  };
  useEffect(() => { load(); const t = setInterval(load, 5000); return () => clearInterval(t); }, []);
  if (!h) return null;
  const where = h.location ? ` · Site: ${h.location}` : "";   // newer hubs say which Site this server is in
  const state = !h.enabled ? "Off" : h.connected ? `Connected as ${h.site_id}${where}${h.org ? ` · ${h.org}` : ""}`
    : h.enrolled ? "Enrolled · reconnecting…" : "Not enrolled";
  return (
    <Row label="Cloud hub" hint="One webpage for all your sites. The site dials out to the hub; no port forwarding. Enter the claim code at the hub under Add site."
      value={<><Status ok={h.connected} /> {state}</>}
      sub={h.last_error && !h.connected ? `Can't reach the hub yet · ${h.last_error}` : h.last_heartbeat ? `last heartbeat ${fmtTime(h.last_heartbeat)}${h.vlm_managed ? " · Qwen managed by the hub" : ""}` : undefined}
      action={h.enrolled ? <button className="ghost small" onClick={async () => { if (await confirmDialog("Unenrol this site from the hub?", { confirmLabel: "Unenrol", danger: true })) { await api.setHub({ unenrol: true }); load(); } }}>Unenrol</button> : undefined}>
      {!h.enrolled && h.claim_code && (
        <div className="hub-claim">
          <div className="hub-code" title="Type this at the hub: Add site">{h.claim_code}</div>
          <div className="muted small">Claim code · renews every 15 minutes</div>
        </div>
      )}
      <div className="row small">
        <input value={url ?? h.hub_url} onChange={(e) => setUrl(e.target.value)} style={{ minWidth: 320 }} title="Hub address (wss://…/agent)" />
        <button className="ghost small" disabled={url == null || url === h.hub_url} onClick={saveUrl}>Save hub URL</button>
      </div>
      <div className="muted small">Changing the hub URL un-enrols this server; it must be claimed again.</div>
      {urlErr && <div className="small error">{urlErr}</div>}
    </Row>
  );
}

export function SystemView() {
  const [s, setS] = useState<SystemInfo | null>(null);
  const [fb, setFb] = useState<FeedbackStats | null>(null);
  useEffect(() => {
    const load = () => {
      api.system().then(setS).catch(() => {});
      api.feedbackStats().then(setFb).catch(() => {});
    };
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, []);
  if (!s) return <div className="view system"><Skeleton lines={4} /><Skeleton lines={3} /></div>;
  const used = 1 - s.recordings_disk.free_gb / s.recordings_disk.total_gb;
  const total = Object.values(s.events).reduce((a, b) => a + b, 0);
  return (
    <div className="view system">
      <Advisor onAsk={(q) => { try { sessionStorage.setItem("findAsk", q); } catch { /* ignore */ } window.dispatchEvent(new CustomEvent("nvr:go", { detail: "Find" })); }} />
      <section className="sys-group">
        <h3>Status</h3>
        <Row label="Recording disk" value={<><strong>{s.recordings_disk.free_gb.toLocaleString()} GB</strong> free of {s.recordings_disk.total_gb.toLocaleString()} GB</>}
          sub={`${s.retention_days} days continuous, then AI-selected`}>
          <div className="meter"><div style={{ width: `${used * 100}%` }} /></div>
        </Row>
        <Row label="YOLO" value={<><Status ok={s.yolo_ready && !s.yolo_fallback} /> {yoloState(s)} · {s.yolo_model}{s.yolo_device ? ` on ${s.yolo_device}` : ""}</>}
          sub={[s.yolo_fallback ? yoloFallbackText(s.yolo_fallback) : "",
            ...(s.health_alerts ?? []).filter((a) => a.kind === "detector_stalled").map((a) => a.text),
            s.queues.verify ? `${s.queues.verify} waiting` : "queue empty", s.yolo_frame_ms != null ? `${s.yolo_frame_ms} ms a frame` : ""].filter(Boolean).join(" · ")} />
        <Row label="Qwen" value={<><Status ok={s.vlm_ready} /> {s.vlm_ready ? "Ready" : s.vlm_state === "unresponsive" ? "Not answering · restarting Ollama" : "Starting"} · {s.vlm_model}</>}
          sub={s.vlm_state === "unresponsive" ? `down since ${s.vlm_down_since ? fmtTime(s.vlm_down_since) : "?"} · if nvidia-smi says the GPU is lost, reboot` : s.queues.synopsis ? `${s.queues.synopsis} waiting` : "queue empty"}
          action={<QwenFeedbackInfo />} />
        <RemoteRow />
        <FootageRow />
        <BackupRow s={s} />
        <HubPanel />
      </section>

      <section className="sys-group">
        <h3>Learning</h3>
        <Row label="Events" value={<strong>{total.toLocaleString()}</strong>} sub={Object.entries(s.events).map(([k, v]) => `${v} ${k}`).join(" · ")} />
        {fb && (
          <Row label="Synopsis feedback" value={<>👍 {fb.up} · 👎 {fb.down} · {fb.corrected} of {fb.synopses} corrected</>}
            sub={Object.keys(fb.reasons).length ? Object.entries(fb.reasons).map(([k, v]) => `${k} ${v}`).join(", ") : "no reasons given yet"}
            action={<a className="small" href={`${BASE}/api/feedback/export`}>Export</a>} />
        )}
        {fb && Object.keys(fb.verdicts).length > 0 && (
          <Row label="Detection verdicts" hint="Camera class : what the operator said it really was"
            value={Object.entries(fb.verdicts).map(([k, v]) => `${k}: ${Object.entries(v).map(([vk, n]) => `${vk.replace("_", " ")} ${n}`).join(", ")}`).join(" · ")} />
        )}
        <BaselineRow />
      </section>

      <RetentionPanel />
    </div>
  );
}

const Status = ({ ok }: { ok: boolean }) => <span className={`dot ${ok ? "ok" : "bad"}`} />;

/** Nightly database copy (backup.py): the part of the NVR that can't be re-recorded. */
function BackupRow({ s }: { s: SystemInfo }) {
  const [busy, setBusy] = useState(false);
  const last = s.backup?.last;
  const run = async () => {
    setBusy(true);
    try { const r = await api.backupNow(); toast.success(`Backed up ${(r.bytes / 1e6).toFixed(1)} MB`); }
    catch (e) { toast.error(e); }
    setBusy(false);
  };
  return (
    <Row label="Database backup" hint="A consistent copy of the database (synopses, feedback, journeys, names, settings) every night at 03:30; the last 14 are kept. Recordings aren't included: they're replaceable, this isn't."
      value={last ? fmtTime(last.at) : "Never"}
      sub={last ? `${(last.bytes / 1e6).toFixed(1)} MB · ${last.count} copies in ${s.backup?.dir}` : `Nightly at 03:30 into ${s.backup?.dir ?? "the backup folder"}`}
      action={<button className="ghost small" disabled={busy} onClick={run}>{busy ? "Backing up…" : "Back up now"}</button>} />
  );
}

const TASK_LABELS: Record<string, string> = {
  assistant: "Ask the NVR", briefing: "Briefings", journey: "Journeys (same person? + narratives)",
  unusual_review: "Unusual-event synopses", footage_verify: "Footage search checks", ppe: "PPE checks (hard hat / vest)",
};

/** Optional larger remote Qwen (e.g. RunPod Serverless): status, which tasks use it, spend, and a test. */
function RemoteRow() {
  const [r, setR] = useState<RemoteStatus | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const load = () => api.remote().then(setR).catch(() => {});
    load();
    const t = setInterval(load, 10000);
    return () => clearInterval(t);
  }, []);
  if (!r) return null;
  const toggle = async (task: string, on: boolean) =>
    setR(await api.remoteTasks(on ? [...r.tasks, task] : r.tasks.filter((t) => t !== task)));
  const test = async () => {
    setBusy(true);
    try {
      const t = await api.remoteTest();
      setR(t.status);
      if (t.ok) toast.success(`Remote model answered in ${t.seconds}s (was ${t.was})`); else toast.error(t.error);
    } catch (e) { toast.error(e); }
    setBusy(false);
  };
  const stateLabel = { off: "Not configured", cold: "Cold (scaled to zero)", warm: "Warm", down: "Down, using local" }[r.state];
  const hint = "A larger Qwen on a rented cloud GPU for reasoning-heavy tasks. Everything falls back to the local model automatically. Configure NVR_REMOTE_VLM_URL, NVR_REMOTE_VLM_KEY and NVR_REMOTE_VLM_MODEL in .env.";
  if (!r.configured) return <Row label="Remote AI" hint={hint} value={stateLabel} sub={`Everything runs on ${r.local_model}`} />;
  return (
    <Row label="Remote AI" hint={hint} value={<><Status ok={r.state === "warm"} /> {stateLabel} · {r.model}</>}
      sub={<>
        {r.today.requests} requests today · ~{Math.round(r.today.billed_s / 60)} GPU-min
        {r.rate_usd_per_s > 0 ? ` · ~$${r.today.usd.toFixed(2)} of $${r.budget_usd.toFixed(2)}` : ""}
        {r.last_latency_s != null ? ` · last ${r.last_latency_s}s` : ""}{r.last_error && r.state === "down" ? ` · ${r.last_error}` : ""}
      </>}
      action={<button className="ghost small" disabled={busy} onClick={test}>{busy ? "Testing…" : "Test"}</button>}>
      <details className="small">
        <summary className="muted">Tasks sent to it</summary>
        <div className="remote-tasks">
          {r.all_tasks.map((t) => (
            <label key={t} className="row small"><input type="checkbox" checked={r.tasks.includes(t)} onChange={(e) => toggle(t, e.target.checked)} /> {TASK_LABELS[t] ?? t}</label>
          ))}
        </div>
      </details>
    </Row>
  );
}

/** Progress of the image-text index behind "Search all footage". */
function FootageRow() {
  const [f, setF] = useState<FootageStatus | null>(null);
  useEffect(() => {
    const load = () => api.footageStatus().then(setF).catch(() => {});
    load();
    const t = setInterval(load, 10000);
    return () => clearInterval(t);
  }, []);
  const ago = (s: number) => (s < 120 ? "up to date" : s < 7200 ? `${Math.round(s / 60)} min behind` : `${(s / 3600).toFixed(1)} h behind`);
  const cams = f ? Object.entries(f.cameras) : [];
  const frames = cams.reduce((a, [, c]) => a + c.frames, 0);
  const worst = cams.reduce((a, [, c]) => Math.max(a, c.cursor ? c.backlog_s : Infinity), 0);
  return (
    <Row label="Footage index" hint="Every few seconds of recording is indexed by what it looks like (OpenCLIP on the YOLO GPU), so Find can search frames no camera event covered. Frames where nothing changed are skipped."
      value={!f ? "…" : <>{frames.toLocaleString()} frames · {Number.isFinite(worst) ? ago(worst) : "starting"}</>}
      sub={f ? `${f.db_mb.toLocaleString()} MB on the recordings drive${f.model_loaded ? "" : " · model loading"}` : undefined} />
  );
}

/** What the NVR has learned is normal per camera (baseline.py); drives the Unusual badge and Priority. */
function BaselineRow() {
  const [b, setB] = useState<BaselineCamera[] | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { api.baseline().then(setB).catch(() => {}); }, []);
  const rebuild = async () => {
    setBusy(true);
    try { const r = await api.rebuildBaseline(); setB(r.cameras); toast.success(`Re-scored ${r.scored} events`); }
    catch (e) { toast.error(e); }
    setBusy(false);
  };
  return (
    <Row label="What's normal" hint="Per camera: what time of day, where in the view, and how long people and vehicles usually stay. Events that break the pattern get an Unusual badge and a higher priority. Rebuilt nightly at 03:00 from the last 4 weeks; false alarms are left out. Time-of-day needs 7 days; place and dwell need 20 events per label."
      value={!b ? "…" : b.map((c) => `${c.camera_id}: ${c.days.toFixed(1)} days, ${Object.values(c.events).reduce((a, n) => a + n, 0)} events, ${c.learning ? "learning" : c.time_active ? "active" : "partly active"}`).join(" · ")}
      action={<button className="ghost small" disabled={busy} onClick={rebuild}>{busy ? "Rebuilding…" : "Rebuild"}</button>} />
  );
}
