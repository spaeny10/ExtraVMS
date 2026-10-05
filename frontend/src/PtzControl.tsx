/**
 * PTZ controls for a live tile (PtzOverlay), the always-on "away" badge (PtzBadge) and the Settings panel
 * (PtzSettings). Talks to backend/nvr/ptz.py. Held controls re-send a ContinuousMove every 0.5 s and Stop on
 * release; the camera itself stops after 2 s if the re-sends stop.
 */
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, type Camera, type PtzInfo, type PtzStatus, type SiteApi } from "./api";
import { contentRect } from "./RegionPaint";
import { confirmDialog, promptDialog, toast } from "./ui";

const SEND_MS = 500;
const DEAD_PX = 12;
type Rect = { left: number; top: number; width: number; height: number };
type Vel = { pan: number; tilt: number; zoom: number };

function statusText(s: PtzStatus | null | undefined): string {
  if (!s) return "";
  if (s.last_error) return `unreachable: ${s.last_error}`;
  if (s.moving) return "moving…";
  if (s.at_home) return `at home (${s.home_name})`;
  if (s.preset_name) return `at '${s.preset_name}'`;
  return s.home_name ? "away from home" : "home preset not set";
}

/** Hold-to-move helper shared by the pad, the drag surface and the keyboard. */
function useHeldMove(cam: string, site: SiteApi) {
  const vel = useRef<Vel | null>(null);
  const timer = useRef<number | undefined>(undefined);
  const send = () => { const v = vel.current; if (v) site.ptzMove(cam, v).catch(() => {}); };
  const start = (v: Vel) => {
    vel.current = v;
    if (timer.current === undefined) { send(); timer.current = window.setInterval(send, SEND_MS); }
  };
  const stop = () => {
    if (timer.current === undefined && !vel.current) return;
    clearInterval(timer.current); timer.current = undefined; vel.current = null;
    site.ptzStop(cam).catch(() => {});
  };
  useEffect(() => () => { clearInterval(timer.current); if (vel.current) site.ptzStop(cam).catch(() => {}); }, [cam, site]);
  return { start, stop, held: () => vel.current !== null };
}

const PAD_KEY = "ptzPad";

/**
 * PTZ mode over a live picture. Control is by mouse/finger on the transparent drag surface (drag = pan/tilt,
 * click = centre, wheel = zoom). A translucent pill in the corner holds presets, relay, input and Done; the
 * on-picture pad (big translucent arrows + zoom) is optional, toggled from the pill and remembered.
 */
export function PtzOverlay({ cam, videoRef, active, onDone, fallbackAspect = 2592 / 1520, site = api }: {
  cam: string; videoRef: React.RefObject<HTMLVideoElement | null>; active: boolean; onDone: () => void; fallbackAspect?: number;
  /** the camera's server (default: this server) */
  site?: SiteApi;
}) {
  const [info, setInfo] = useState<PtzInfo | null>(null);
  const [pad, setPad] = useState(() => { try { return localStorage.getItem(PAD_KEY) === "1"; } catch { return false; } });
  const togglePad = () => setPad((p) => { try { localStorage.setItem(PAD_KEY, p ? "0" : "1"); } catch { /* private mode */ } return !p; });
  const [rect, setRect] = useState<Rect | null>(null);
  const surface = useRef<HTMLDivElement>(null);
  const drag = useRef<{ x0: number; y0: number; moved: boolean } | null>(null);
  const lastWheel = useRef(0);
  const { start, stop } = useHeldMove(cam, site);
  const refresh = () => site.ptz(cam).then(setInfo).catch((e) => setInfo((i) => i ? { ...i, status: { ...i.status, last_error: String(e) } } : i));

  useEffect(() => {
    if (!active) { stop(); return; }
    refresh();
    const t = setInterval(refresh, 2000);
    surface.current?.focus();
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, cam]);

  useLayoutEffect(() => {
    const el = surface.current?.parentElement;
    if (!el || !active) return;
    const measure = () => {
      const v = videoRef.current;
      const aspect = v && v.videoWidth && v.videoHeight ? v.videoWidth / v.videoHeight : fallbackAspect;
      setRect(contentRect({ width: el.clientWidth, height: el.clientHeight }, aspect));
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    const v = videoRef.current;
    v?.addEventListener("loadedmetadata", measure);
    v?.addEventListener("resize", measure);
    return () => { ro.disconnect(); v?.removeEventListener("loadedmetadata", measure); v?.removeEventListener("resize", measure); };
  }, [videoRef, fallbackAspect, active]);

  if (!active) return null;
  const s = info?.status;
  const panTilt = s?.pan_tilt !== false;
  const stopAll = (e: React.SyntheticEvent) => e.stopPropagation();
  const after = (p: Promise<unknown>) => p.then(refresh).catch((e) => toast.error(e));
  const clamp = (v: number) => Math.max(-1, Math.min(1, v));
  const hold = (v: Vel) => ({
    onPointerDown: (e: React.PointerEvent) => { e.stopPropagation(); e.preventDefault(); start(v); },
    onPointerUp: stop, onPointerLeave: stop, onPointerCancel: stop,
  });

  const saveAs = async () => {
    const name = await promptDialog("Save this view as a preset", { label: "Name", confirmLabel: "Save" });
    if (name?.trim()) after(site.ptzSavePreset(cam, name.trim()).then(() => toast.success(`Preset "${name.trim()}" saved`)));
  };

  return (
    <>
      {panTilt && <div ref={surface} className="ptz-surface" tabIndex={0} title="Drag to pan/tilt · click to centre · wheel to zoom · arrow keys"
        style={rect ? { left: rect.left, top: rect.top, width: rect.width, height: rect.height } : undefined}
        onClick={stopAll} onDoubleClick={stopAll} onContextMenu={(e) => { e.preventDefault(); e.stopPropagation(); }}
        onPointerDown={(e) => {
          e.stopPropagation(); e.preventDefault();
          e.currentTarget.setPointerCapture(e.pointerId);
          e.currentTarget.focus();
          drag.current = { x0: e.clientX, y0: e.clientY, moved: false };
        }}
        onPointerMove={(e) => {
          const d = drag.current;
          if (!d || !rect) return;
          const dx = e.clientX - d.x0, dy = e.clientY - d.y0;
          if (!d.moved && Math.hypot(dx, dy) < DEAD_PX) return;
          d.moved = true;
          start({ pan: clamp(dx / (rect.width / 2)), tilt: clamp(-dy / (rect.height / 2)), zoom: 0 });
        }}
        onPointerUp={(e) => {
          const d = drag.current; drag.current = null;
          if (!d) return;
          if (d.moved) { stop(); return; }
          const r = e.currentTarget.getBoundingClientRect();
          after(site.ptzRelative(cam, { dx: (e.clientX - r.left) / r.width - 0.5, dy: (e.clientY - r.top) / r.height - 0.5 }));
        }}
        onPointerCancel={() => { drag.current = null; stop(); }}
        onWheel={(e) => {
          e.preventDefault(); e.stopPropagation();
          const now = Date.now();
          if (now - lastWheel.current < 150) return;
          lastWheel.current = now;
          after(site.ptzRelative(cam, { zoom: e.deltaY < 0 ? 0.05 : -0.05 }));
        }}
        onKeyDown={(e) => {
          if (e.repeat) return;
          const v: Record<string, Vel> = { ArrowLeft: { pan: -0.5, tilt: 0, zoom: 0 }, ArrowRight: { pan: 0.5, tilt: 0, zoom: 0 },
            ArrowUp: { pan: 0, tilt: 0.5, zoom: 0 }, ArrowDown: { pan: 0, tilt: -0.5, zoom: 0 } };
          if (v[e.key]) { e.preventDefault(); start(v[e.key]); }
          else if (e.key === "+" || e.key === "=") after(site.ptzRelative(cam, { zoom: 0.05 }));
          else if (e.key === "-") after(site.ptzRelative(cam, { zoom: -0.05 }));
          else if (e.key === "Home") after(site.ptzHome(cam));
          else if (e.key === "Escape") onDone();
        }}
        onKeyUp={(e) => { if (e.key.startsWith("Arrow")) stop(); }}
      />}
      {pad && panTilt && (
        <div className="ptz-padlay" style={rect ? { left: rect.left, top: rect.top, width: rect.width, height: rect.height } : undefined}
          onPointerDown={stopAll} onClick={stopAll} onDoubleClick={stopAll} onWheel={stopAll}>
          <button className="ptz-arrow up" title="Tilt up (hold)" {...hold({ pan: 0, tilt: 0.5, zoom: 0 })}>▲</button>
          <button className="ptz-arrow down" title="Tilt down (hold)" {...hold({ pan: 0, tilt: -0.5, zoom: 0 })}>▼</button>
          <button className="ptz-arrow left" title="Pan left (hold)" {...hold({ pan: -0.5, tilt: 0, zoom: 0 })}>◀</button>
          <button className="ptz-arrow right" title="Pan right (hold)" {...hold({ pan: 0.5, tilt: 0, zoom: 0 })}>▶</button>
          <button className="ptz-arrow home" title="Go home" onClick={() => after(site.ptzHome(cam))}>⌂</button>
          <div className="ptz-zoom">
            <button title="Zoom in (hold)" {...hold({ pan: 0, tilt: 0, zoom: 0.5 })}>+</button>
            <button title="Zoom out (hold)" {...hold({ pan: 0, tilt: 0, zoom: -0.5 })}>−</button>
          </div>
        </div>
      )}
      <div className="ptz-pill" onPointerDown={stopAll} onClick={stopAll} onDoubleClick={stopAll} onWheel={stopAll}>
        {!panTilt && (
          <div className="segmented small-seg" title="Zoom (hold)">
            <button {...hold({ pan: 0, tilt: 0, zoom: -0.5 })}>−</button>
            <button {...hold({ pan: 0, tilt: 0, zoom: 0.5 })}>+</button>
          </div>
        )}
        {panTilt && <select value="" title="Go to a preset" onChange={(e) => {
          const v = e.target.value;
          if (v === "__save") saveAs(); else if (v) after(site.ptzGoto(cam, v));
        }}>
          <option value="">{s?.preset_name ? `at ${s.preset_name}` : "Preset…"}</option>
          {info?.presets.filter((p) => !p.system).map((p) => <option key={p.token} value={p.token}>{p.is_home ? "★ " : ""}{p.name}</option>)}
          <option value="__save">＋ Save current view…</option>
          {info?.presets.some((p) => p.system) && (
            <optgroup label="Camera">{info.presets.filter((p) => p.system).map((p) => <option key={p.token} value={p.token}>{p.name}</option>)}</optgroup>
          )}
        </select>}
        {s?.relay && (
          <button className={`ghost small ${s.relay.state ? "on" : ""}`} title={`Relay output (${s.relay.mode})`}
            onClick={() => after(site.relay(cam, s.relay!.mode === "monostable" ? true : !s.relay!.state))}>
            ⚡ {s.relay.label}{s.relay.mode === "monostable" || s.relay.state == null ? "" : s.relay.state ? " · on" : " · off"}
          </button>
        )}
        {s?.input && <span className={`ptz-chip ${s.input.state ? "on" : ""}`} title={`Digital input: ${s.input.state == null ? "unknown" : s.input.state ? "active" : "idle"}`}>⏺ {s.input.label}</span>}
        {panTilt && <button className={`ghost small ${pad ? "on" : ""}`} title={pad ? "Hide the on-screen pad (drag the picture to move, wheel to zoom)" : "Show an on-screen pad for pan/tilt/zoom"} onClick={togglePad}>✚</button>}
        {s?.last_error && <span className="ptz-chip on" title={s.last_error}>unreachable</span>}
        <button className="small" title="Leave PTZ mode (Esc)" onClick={onDone}>Done</button>
      </div>
    </>
  );
}

/** Tile-bar badge: where a PTZ camera points when it isn't home, plus relay/input state. */
export function PtzBadge({ cam, ptz, site = api }: { cam: string; ptz: PtzStatus | null | undefined; site?: SiteApi }) {
  if (!ptz?.available) return null;
  const away = ptz.home_name && !ptz.at_home;
  const bits: React.ReactNode[] = [];
  if (away) bits.push(
    <span key="away" className="ptz-badge" title="The camera is turned away from its home view: zones, places and the learned baseline don't apply until it returns">
      ↗ {ptz.moving ? "moving" : ptz.preset_name ?? "away"}
      <button className="linkish" title={`Return to home (${ptz.home_name})`} onClick={(e) => { e.stopPropagation(); site.ptzHome(cam).then(() => toast.info("Returning home")).catch((err) => toast.error(err)); }}>Home</button>
    </span>);
  if (ptz.relay?.state) bits.push(<span key="relay" className="ptz-badge" title="Relay output is on">⚡ {ptz.relay.label}</span>);
  if (ptz.input?.state) bits.push(<span key="input" className="ptz-badge" title="Digital input is active">⏺ {ptz.input.label}</span>);
  return <>{bits}</>;
}

/** Settings → Cameras: home preset, return-home timer, labels, the preset list and a re-probe. */
export function PtzSettings({ camera, onChanged }: { camera: Camera; onChanged?: () => void }) {
  const [info, setInfo] = useState<PtzInfo | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const load = () => api.ptz(camera.id).then((i) => { setInfo(i); setErr(""); }).catch((e) => { setInfo(null); setErr(String(e)); });
  useEffect(() => { load(); }, [camera.id]); // eslint-disable-line react-hooks/exhaustive-deps
  const run = async (p: Promise<unknown>, ok?: string) => {
    setBusy(true);
    try { await p; if (ok) toast.success(ok); await load(); onChanged?.(); }
    catch (e) { toast.error(e); }
    setBusy(false);
  };
  if (!info) {
    return (
      <div className="ptz-settings">
        <p className="muted small">{err.includes("409") ? "No PTZ found on this camera." : err ? `Not available: ${err}` : "Checking…"}</p>
        <button className="ghost small" disabled={busy} onClick={() => run(api.ptzProbe(camera.id))}>Probe camera</button>
      </div>
    );
  }
  const c = info.config, s = info.status, caps: NonNullable<PtzInfo["caps"]> = info.caps ?? { available: true };
  const user = info.presets.filter((p) => !p.system);
  if (caps.pan_tilt === false) {
    return (
      <div className="ptz-settings">
        <p className="muted small">Fixed camera with a motorised lens: zoom and focus only, no pan/tilt or presets, so the home-view rules don't apply.
          {s.relay ? ` Relay output (${s.relay.mode}).` : ""}{s.input ? " Digital input." : ""} Zoom, relay and input are on the tile's 🕹 button.</p>
        <div className="row">
          {s.relay && <label className="field"><span>Relay label</span>
            <input defaultValue={c.relay_label} disabled={busy} onBlur={(e) => { if (e.target.value.trim() && e.target.value.trim() !== c.relay_label) run(api.ptzConfig(camera.id, { relay_label: e.target.value.trim() })); }} /></label>}
          {s.input && <label className="field"><span>Digital input label</span>
            <input defaultValue={c.input_label} disabled={busy} onBlur={(e) => { if (e.target.value.trim() && e.target.value.trim() !== c.input_label) run(api.ptzConfig(camera.id, { input_label: e.target.value.trim() })); }} /></label>}
          <button className="ghost small" disabled={busy} onClick={() => run(api.ptzProbe(camera.id), "Camera re-probed")}>Probe camera</button>
        </div>
      </div>
    );
  }
  return (
    <div className="ptz-settings">
      <p className="muted small">
        {statusText(s)} · {caps.max_presets} presets max{caps.home_supported ? " · home" : ""}{caps.tours ? " · tours" : ""}
        {s.relay ? ` · relay (${s.relay.mode})` : ""}{s.input ? " · digital input" : ""}
        {caps.aux_commands?.length ? ` · aux: ${caps.aux_commands.join(", ")}` : ""}
      </p>
      <label className="field">
        <span>Home preset</span>
        <select value={c.home_token ?? ""} disabled={busy} onChange={(e) => run(api.ptzSetHome(camera.id, e.target.value || null), e.target.value ? "Home preset set (the camera moved there to learn the position)" : "Home preset cleared")}>
          <option value="">— none (analytics apply everywhere) —</option>
          {user.map((p) => <option key={p.token} value={p.token}>{p.name}</option>)}
        </select>
        <span className="small">Zones, named places, painted regions and "what's normal" apply only at the home view. Events while the camera is turned elsewhere are still recorded, tagged with the preset name.</span>
      </label>
      <div className="row">
        <button className="ghost small" disabled={busy} onClick={async () => {
          const name = await promptDialog("Save the current view as the home preset", { label: "Preset name", initial: c.home_name ?? "Home", confirmLabel: "Save & make home" });
          if (!name?.trim()) return;
          await run(api.ptzSavePreset(camera.id, name.trim()).then((r) => api.ptzSetHome(camera.id, r.token)), "Current view saved as home");
        }}>Set current position as home</button>
      </div>
      <div className="row">
        <label className="field"><span>Return home after (min, 0 = never)</span>
          <input type="number" min={0} max={1440} defaultValue={c.return_home_min} disabled={busy}
            onBlur={(e) => { const v = Math.max(0, Math.min(1440, +e.target.value || 0)); if (v !== c.return_home_min) run(api.ptzConfig(camera.id, { return_home_min: v })); }} /></label>
        {s.relay && <label className="field"><span>Relay label</span>
          <input defaultValue={c.relay_label} disabled={busy} onBlur={(e) => { if (e.target.value.trim() && e.target.value.trim() !== c.relay_label) run(api.ptzConfig(camera.id, { relay_label: e.target.value.trim() })); }} /></label>}
        {s.input && <label className="field"><span>Digital input label</span>
          <input defaultValue={c.input_label} disabled={busy} onBlur={(e) => { if (e.target.value.trim() && e.target.value.trim() !== c.input_label) run(api.ptzConfig(camera.id, { input_label: e.target.value.trim() })); }} /></label>}
      </div>
      <div className="field"><span>Presets</span>
        {info.presets.map((p) => (
          <div key={p.token} className="preset-row">
            <span>{p.is_home ? "★ " : ""}{p.name}{p.system ? <span className="muted small"> · camera</span> : !p.known ? <span className="muted small" title="Position not learned yet: go to it once and the NVR will recognise it"> · not learned</span> : null}</span>
            <span className="spacer" />
            <button className="linkish" disabled={busy} onClick={() => run(api.ptzGoto(camera.id, p.token, true))}>Go to</button>
            {!p.system && <button className="linkish" disabled={busy} onClick={async () => {
              const name = await promptDialog(`Rename "${p.name}"`, { label: "New name", initial: p.name, message: "The camera will move to this preset first (ONVIF re-saves the position with the name).", confirmLabel: "Rename" });
              if (name?.trim() && name.trim() !== p.name) run(api.ptzRenamePreset(camera.id, p.token, name.trim()), "Preset renamed");
            }}>Rename</button>}
            {!p.system && !p.is_home && <button className="linkish" disabled={busy} onClick={async () => {
              if (await confirmDialog(`Delete preset "${p.name}"?`, { confirmLabel: "Delete", danger: true })) run(api.ptzDeletePreset(camera.id, p.token), "Preset deleted");
            }}>Delete</button>}
          </div>
        ))}
      </div>
      <div className="row"><button className="ghost small" disabled={busy} onClick={() => run(api.ptzProbe(camera.id), "Camera re-probed")}>Probe camera</button></div>
    </div>
  );
}
