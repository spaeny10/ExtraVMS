/* Settings → Cameras → Add / Edit camera: a near-full-screen panel with one tab per kind of setting, the header summing
   up the camera, and Save / Cancel always at the bottom. Which tab a field is on, the header and the opening tab are pure
   helpers in cameraEdit.ts. Sub-editors that save on their own (neighbors, PTZ, the zone editor) keep their own buttons. */
import { cloneElement, isValidElement, useEffect, useId, useRef, useState } from "react";
import { api, type Camera, type EventSource, type RetentionPolicy, type ScannedDevice, type Speaker, type Zone } from "./api";
import { NeighborsEditor } from "./Neighbors";
import { PolicyForm } from "./RetentionPanel";
import { PtzSettings } from "./PtzControl";
import { confirmDialog, errorText, toast, useIsPhone } from "./ui";
import { detectionsText, streamsText, streamsTitle, suggestText } from "./cameraStreams";
import { CAMERA_TABS, dirtyTabs, headerSummary, openingTab, visibleTabs, type CameraDraft, type CameraTab } from "./cameraEdit";

export const blankCamera: CameraDraft = {
  id: "", name: "", host: "", onvif_port: 80, rtsp_port: 554, username: "admin", main_path: "/main",
  sub_path: "/sub", enabled: true, zones: [], retention_days: null, scene_notes: "",
};

/** The tab this viewer had open last (this session): the next camera opens on it. */
let lastTab: CameraTab | null = null;

export function CameraDialog({ draft, cameras, sitePolicy, reload, onClose, onEditZones }: {
  draft: CameraDraft; cameras: Camera[]; sitePolicy: RetentionPolicy | null; reload: () => void;
  onClose: () => void; onEditZones: (c: Camera) => void;
}) {
  // new or existing is decided when the dialog opens (typing an existing ID into a new camera doesn't turn it into an edit)
  const [isNew] = useState(() => !cameras.some((c) => c.id === draft.id));
  const [initial, setInitial] = useState(draft);
  const [edit, setEdit] = useState(draft);
  const [err, setErr] = useState("");
  const saved = isNew ? undefined : cameras.find((c) => c.id === edit.id);
  const tabs = visibleTabs(Boolean(saved?.status?.ptz?.available));
  const [tab, setTabState] = useState<CameraTab>(() => openingTab(lastTab, isNew, tabs));
  const shown = tabs.includes(tab) ? tab : "general";
  const setTab = (t: CameraTab) => { lastTab = t; setTabState(t); };
  const dirty = dirtyTabs(initial, edit);
  const phone = useIsPhone();
  const uid = useId();
  const tabRefs = useRef<Partial<Record<CameraTab, HTMLButtonElement | null>>>({});

  // the speakers bound to this camera (read-only here; bound in Speakers below the camera list)
  const [speakers, setSpeakers] = useState<Speaker[] | null>(null);
  useEffect(() => {
    if (!isNew) api.speakers().then((l) => setSpeakers(l.filter((s) => s.camera_ids?.includes(draft.id)))).catch(() => setSpeakers(null));
  }, [isNew, draft.id]);

  /** True when there is nothing unsaved, or the viewer chose to discard it. */
  const asking = useRef(false);
  const mayDiscard = async () => {
    if (!dirty.size) return true;
    if (asking.current) return false;
    asking.current = true;
    try { return await confirmDialog("Discard your changes?", { confirmLabel: "Discard", danger: true }); }
    finally { asking.current = false; }
  };
  const requestClose = async () => { if (await mayDiscard()) onClose(); };
  // Esc closes (captured first, so an in-app confirm or prompt dialog on top gets its own Esc instead)
  const closeRef = useRef(requestClose);
  closeRef.current = requestClose;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || document.querySelector(".dialog-backdrop")) return;
      closeRef.current();
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, []);

  const save = async () => {
    setErr("");
    try {
      await api.saveCamera({ ...edit, password: edit.password || undefined });
      onClose();
      reload();
      toast.success(`Camera "${edit.name}" saved`);
    } catch (e) {
      setErr(errorText(e));
      toast.error(e);
    }
  };

  // arrow keys move between tabs (up/down beside the panel, left/right above it on phones)
  const onTabKey = (e: React.KeyboardEvent) => {
    const step = { ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1 }[e.key];
    const i = tabs.indexOf(shown);
    const next = step ? tabs[(i + step + tabs.length) % tabs.length] : e.key === "Home" ? tabs[0] : e.key === "End" ? tabs[tabs.length - 1] : null;
    if (!next) return;
    e.preventDefault();
    setTab(next);
    tabRefs.current[next]?.focus();
  };

  const summary = headerSummary(saved);
  const title = isNew ? "Add camera" : `Edit ${saved?.name ?? edit.name}`;
  const label = (t: CameraTab) => CAMERA_TABS.find((x) => x.id === t)!.label;
  const tabId = (t: CameraTab) => `${uid}-tab-${t}`;
  const panelId = (t: CameraTab) => `${uid}-panel-${t}`;
  // every tab stays mounted (hidden when not shown): sub-editors keep what was typed, each tab keeps its scroll
  const panel = (t: CameraTab, body: React.ReactNode) => (
    <div key={t} className="cam-panel form" role="tabpanel" id={panelId(t)} aria-labelledby={tabId(t)} hidden={t !== shown}>{body}</div>
  );

  return (
    <div className="modal-backdrop cam-backdrop" onClick={requestClose}>
      <div className="modal cam-modal" role="dialog" aria-modal="true" aria-label={title} onClick={(e) => e.stopPropagation()}>
        <header className="modal-head cam-head">
          <div className="cam-head-text">
            <h2>{summary[0].text}</h2>
            {summary.length > 1 && (
              <div className="cam-summary small muted">
                {summary.slice(1).map((p, i) => (
                  <span key={i}>{i > 0 && " · "}{p.tone && <span className={`dot ${p.tone}`} aria-hidden="true" />}{p.tone && " "}{p.text}</span>
                ))}
              </div>
            )}
          </div>
          <button className="ghost" onClick={requestClose} aria-label="Close">✕</button>
        </header>
        <div className="cam-body">
          <div className="cam-tabs" role="tablist" aria-label="Camera settings" aria-orientation={phone ? "horizontal" : "vertical"} onKeyDown={onTabKey}>
            {tabs.map((t) => (
              <button key={t} id={tabId(t)} ref={(el) => { tabRefs.current[t] = el; }} role="tab" aria-selected={t === shown}
                aria-controls={panelId(t)} tabIndex={t === shown ? 0 : -1} className={`cam-tab ${t === shown ? "active" : ""}`} onClick={() => setTab(t)}>
                {label(t)}
                {dirty.has(t) && <span className="tab-dot" title="Unsaved changes"><span className="sr-only"> (unsaved changes)</span></span>}
              </button>
            ))}
          </div>
          {panel("general", <>
            <Field label="Name"><input value={edit.name} onChange={(e) => setEdit({ ...edit, name: e.target.value })} /></Field>
            <div className="row">
              <Field label="Enabled"><input type="checkbox" checked={Boolean(edit.enabled)} onChange={(e) => setEdit({ ...edit, enabled: e.target.checked })} /></Field>
            </div>
            <Field label="ID" hint={isNew ? undefined : "Set when the camera is added; it can't be changed."}>
              <input value={edit.id} disabled={!isNew} onChange={(e) => setEdit({ ...edit, id: e.target.value })} />
            </Field>
            <Field label="Scene notes for Qwen" hint="Added to every synopsis and chat prompt for this camera.">
              <textarea rows={5} value={edit.scene_notes ?? ""} onChange={(e) => setEdit({ ...edit, scene_notes: e.target.value })}
                placeholder={"What's normal in this view, e.g.\nThe white trailers in the foreground are our solar light towers.\nThe road and Ford dealership in the background are public; traffic there is routine.\nStaff wear hi-vis vests."} />
            </Field>
          </>)}

          {panel("connection", <>
            {isNew && (
              <ScanBox cameras={cameras} username={edit.username} password={edit.password}
                onUse={(d) => setEdit({ ...edit, host: d.address, onvif_port: d.port, name: edit.name || [d.manufacturer, d.model].filter(Boolean).join(" ") })} />
            )}
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
            {saved
              ? <StreamsLine camera={saved} onChecked={reload}
                  // "Use …" saves the paths itself: they are no longer unsaved changes
                  onPaths={(p) => { setEdit({ ...edit, ...p }); setInitial((i) => ({ ...i, ...p })); }} />
              : <Hint text="The camera is asked which streams it serves when it is saved."
                  more="The camera is asked which streams it serves when it is saved. Without a low-resolution stream, SD live view plays the main stream." />}
            <details className="field" open={Boolean(initial.public_host)}>
              <summary>Advanced: Port forwarding (outside address)</summary>
              <Field label="Outside address"><input value={edit.public_host ?? ""} placeholder="the site router's public IP or name"
                onChange={(e) => setEdit({ ...edit, public_host: e.target.value.trim() || null })} /></Field>
              <div className="row">
                <Field label="Outside ONVIF port"><input type="number" value={edit.public_onvif_port ?? ""} placeholder={String(edit.onvif_port)}
                  onChange={(e) => setEdit({ ...edit, public_onvif_port: e.target.value ? +e.target.value : null })} /></Field>
                <Field label="Outside RTSP port"><input type="number" value={edit.public_rtsp_port ?? ""} placeholder={String(edit.rtsp_port)}
                  onChange={(e) => setEdit({ ...edit, public_rtsp_port: e.target.value ? +e.target.value : null })} /></Field>
                <Field label="Outside replay port (SD card)"><input type="number" value={edit.public_replay_port ?? ""} placeholder="555"
                  onChange={(e) => setEdit({ ...edit, public_replay_port: e.target.value ? +e.target.value : null })} /></Field>
              </div>
              <Hint text="Only for a camera reached through the site router's port forwards; leave empty on the same network or over a VPN."
                more="For a camera reached through the site router's port forwards. This server connects to the outside address and ports, and the addresses the camera reports (events, PTZ, streams) are rewritten to them. Host / IP and the ports above stay the camera's own. Leave empty on the same network or over a VPN." />
            </details>
            {saved && !tabs.includes("ptz") && (
              <details className="field">
                <summary>PTZ (not detected)</summary>
                <SavesNow />
                <PtzSettings camera={saved} onChanged={reload} />
              </details>
            )}
          </>)}

          {panel("recording", <>
            <Field label="Record continuously" hint="Sub stream saves cellular data; HD live view still pulls the main stream while someone watches."
              more="Sub stream saves cellular data: 5 cameras recording 4K continuously upload about 20-30 Mbit/s (7-10 TB a month). With the sub stream, recordings, clips and detection use the smaller picture; HD live view still pulls the main stream while someone watches.">
              <select value={edit.record_stream ?? "main"} onChange={(e) => setEdit({ ...edit, record_stream: e.target.value as "main" | "sub" })}>
                <option value="main">Main stream (full resolution)</option>
                <option value="sub">Sub stream (saves cellular data)</option>
              </select>
            </Field>
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
          </>)}

          {panel("detection", <>
            {(() => {
              const d = saved?.status?.detections;
              const note = detectionsText(d);
              const now = note ? `${note}: ${d?.reason}.` : d ? `Detections: from the camera's object metadata (${d.reason}).` : "";
              return (
                <Field label="Events from" hint={now || "Automatic: the camera's object metadata, or its ONVIF detection events when it sends no objects."}
                  more="Automatic uses the camera's ONVIF detection events (e.g. Reolink people detection) only when it sends no objects in its metadata; those events have no positions, so YOLO finds the person or vehicle and the zones apply to what it finds.">
                  <select value={edit.event_source ?? "auto"} onChange={(e) => setEdit({ ...edit, event_source: e.target.value as EventSource })}>
                    <option value="auto">Automatic</option>
                    <option value="metadata">The camera's object metadata</option>
                    <option value="onvif_events">The camera's ONVIF events (people / vehicle detection)</option>
                  </select>
                </Field>
              );
            })()}
            {(edit.event_source ?? "auto") !== "metadata" && (
              <label className="row small">
                <input type="checkbox" checked={Boolean(edit.motion_events)} onChange={(e) => setEdit({ ...edit, motion_events: e.target.checked })} />
                Open events on the camera's motion alarms too (YOLO must confirm a person or vehicle)
              </label>
            )}
            <div className="field" role="group" aria-labelledby={`${uid}-qwen`} aria-describedby={`${uid}-qwen-hint`}>
              <span id={`${uid}-qwen`}>Qwen describes</span>
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
              <Hint id={`${uid}-qwen-hint`} text="YOLO-verified detections only, and only inside this camera's zones."
                more="YOLO-verified detections only, and only inside this camera's zones. Unusual events are described either way." />
            </div>
            <h3 className="spaced">Detection zones</h3>
            <p className="cam-zones">{zoneSummary(edit.zones)}</p>
            <Hint text="Mask out busy areas such as a road, or limit detection to the areas you care about."
              more="Mask out busy areas such as a road, or limit detection to the areas you care about. Drawn on a full-resolution still with recent detections shown." />
            {saved ? (
              <div className="row">
                <button className="ghost" onClick={async () => { if (await mayDiscard()) onEditZones(saved); }}>Edit zones…</button>
                <span className="muted small">Opens the zone editor in place of this dialog; zones save there.</span>
              </div>
            ) : (
              <p className="muted small">Save the camera first.</p>
            )}
          </>)}

          {panel("journeys", <>
            <h3>Neighboring cameras</h3>
            {saved ? <><SavesNow what="Neighbors save with their own “Save neighbors” button, not with Save below." /><NeighborsEditor cameraId={saved.id} cameras={cameras} /></>
              : <p className="muted small">Save the camera first.</p>}
          </>)}

          {saved && tabs.includes("ptz") && panel("ptz", <>
            <SavesNow />
            <PtzSettings camera={saved} onChanged={reload} />
          </>)}

          {panel("rules", <>
            <div className="field" role="group" aria-labelledby={`${uid}-rules`} aria-describedby={`${uid}-rules-hint`}>
              <span id={`${uid}-rules`}>Site rules</span>
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
              <Hint id={`${uid}-rules-hint`} text="Flag a vehicle towing the asset, or a person entering through a named place, that isn't named in People & vehicles."
                more="Towing: checked after Qwen describes each vehicle here — a vehicle towing the asset that isn't a named vehicle is flagged. Entry: checked right after verification — a person whose track starts at that named place (an exterior door) who isn't a named person is flagged. Both raise the event to that priority and show it under Needs attention. Name your own people and trucks in People & vehicles first; an unnamed one trips the rule once." />
            </div>
            {speakers && <>
              <h3 className="spaced">Talk-down speakers</h3>
              {speakers.length ? (
                <ul className="cam-speakers">
                  {speakers.map((s) => (
                    <li key={s.id} className={`small ${s.enabled ? "" : "muted"}`}>
                      <span className={`dot ${s.check?.ok ? "ok" : s.checked_at ? "bad" : "idle"}`} aria-hidden="true" /> <strong>{s.name}</strong>{" "}
                      <span className="muted">{s.host}{s.enabled ? "" : " · disabled"}</span>
                    </li>
                  ))}
                </ul>
              ) : <p className="muted small">No speaker is bound to this camera.</p>}
              <p className="muted small">Speakers are added and bound to cameras under Speakers, below the camera list.</p>
            </>}
          </>)}
        </div>
        <footer className="cam-foot">
          {err ? <span className="error small cam-foot-msg">{err}</span>
            : <span className="muted small cam-foot-msg">{dirty.size ? "Unsaved changes" : ""}</span>}
          <span className="spacer" />
          <button className="ghost" onClick={requestClose}>Cancel</button>
          <button onClick={save}>Save</button>
        </footer>
      </div>
    </div>
  );
}

/** A note on a tab whose settings save as soon as they change, not with the dialog's Save. */
function SavesNow({ what = "Changes here save immediately, not with Save below." }: { what?: string }) {
  return <p className="saves-now small">{what}</p>;
}

/** The one-line hint under a field; the full explanation behind "?" (both part of the field's description). */
function Hint({ id, text, more }: { id?: string; text: React.ReactNode; more?: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  const own = useId();
  const hid = id ?? own;
  return (
    <div className="hint small" id={hid}>
      <span>{text}</span>
      {more && <>
        {" "}<button type="button" className="hint-q" aria-expanded={open} aria-controls={`${hid}-more`}
          aria-label={open ? "Less about this" : "More about this"} title={open ? "Less" : "More about this"} onClick={() => setOpen(!open)}>?</button>
        <div className="hint-more" id={`${hid}-more`} hidden={!open}>{more}</div>
      </>}
    </div>
  );
}

/** A labelled field; with `hint`, the hint (and `more` behind "?") describes the control. */
function Field({ label, hint, more, children }: { label: string; hint?: React.ReactNode; more?: React.ReactNode; children: React.ReactNode }) {
  const id = useId();
  if (!hint) {
    return (
      <label className="field">
        <span>{label}</span>
        {children}
      </label>
    );
  }
  const control = isValidElement(children) ? cloneElement(children as React.ReactElement<{ "aria-describedby"?: string }>, { "aria-describedby": id }) : children;
  return (
    <div className="field">
      <label className="field">
        <span>{label}</span>
        {control}
      </label>
      <Hint id={id} text={hint} more={more} />
    </div>
  );
}


/** Settings → Cameras: the streams the camera serves (its ONVIF profiles, backend streams.py); "Check" asks it now. */
export function StreamsLine({ camera, onChecked, onPaths }: { camera: Camera; onChecked: () => void; onPaths?: (p: { main_path: string; sub_path: string }) => void }) {
  const [busy, setBusy] = useState(false);
  const s = camera.streams;
  const text = streamsText(s);
  const fix = suggestText(s, camera);
  // the configured main path is not one the camera lists: one click saves the paths it does list (never automatic)
  const usePaths = async () => {
    if (!s?.suggest) return;
    setBusy(true);
    try {
      const { status, streams, ...rest } = camera;
      await api.saveCamera({ ...rest, main_path: s.suggest.main_path, sub_path: s.suggest.sub_path });
      toast.success(`${camera.name}: now ${s.suggest.main_path} and ${s.suggest.sub_path}`);
      onPaths?.(s.suggest);
      onChecked();
    } catch (e) {
      toast.error(e);
    } finally {
      setBusy(false);
    }
  };
  const check = async () => {
    setBusy(true);
    try {
      const r = await api.checkStreams(camera.id);
      if (r.pending) toast.success(`${camera.name}: the camera is slow to answer; its streams will show when it does`);
      else if (r.error) toast.error(`${camera.name}: ${r.error}`);
      else toast.success(`${camera.name}: ${streamsText(r) || "no video streams listed"}`);
      onChecked();
    } catch (e) {
      toast.error(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className={`small ${s?.error && !text ? "muted" : ""}`} title={streamsTitle(s)}>
      Streams: {text || (s?.error ? "check failed" : s?.checked_at ? "none listed" : "not checked")}{" "}
      <button className="ghost small" disabled={busy} onClick={check} title="Ask the camera which streams it serves">{busy ? "…" : "Check"}</button>
      {fix && <button className="small" disabled={busy} onClick={usePaths}
        title="The camera does not list the configured main stream path: save the paths it lists (largest stream as main)">{fix}</button>}
    </div>
  );
}

export function zoneSummary(zones: Zone[]): string {
  const valid = zones.filter((z) => z.points.length >= 3);
  if (!valid.length) return "Full frame";
  const count = (t: string) => valid.filter((z) => (z.type ?? "include") === t).length;
  const masks = count("exclude"), areas = count("include"), places = count("area"), ppe = count("ppe");
  return [masks && `${masks} mask${masks > 1 ? "s" : ""}`, areas && `${areas} detect-only area${areas > 1 ? "s" : ""}`,
    places && `${places} place${places > 1 ? "s" : ""}`, ppe && `${ppe} PPE zone${ppe > 1 ? "s" : ""}`].filter(Boolean).join(", ");
}

/** A guess at the subnet to scan: the /24 of the first camera with an IPv4 address. */
function guessSubnet(cameras: Camera[]): string {
  const ip = cameras.map((c) => /^(\d+\.\d+\.\d+)\.\d+$/.exec(c.host)?.[1]).find(Boolean);
  return ip ? `${ip}.0/24` : "";
}

/** Unicast ONVIF scan (POST /api/cameras/scan): finds cameras where WS-Discovery can't (over a VPN tunnel). */
function ScanBox({ cameras, username, password, onUse }: {
  cameras: Camera[]; username?: string; password?: string; onUse: (d: ScannedDevice) => void;
}) {
  const [subnet, setSubnet] = useState(() => guessSubnet(cameras));
  const [ports, setPorts] = useState("80, 8000, 8080");
  const [busy, setBusy] = useState(false);
  const [found, setFound] = useState<ScannedDevice[] | null>(null);
  const [err, setErr] = useState("");
  const scan = async () => {
    setBusy(true); setErr(""); setFound(null);
    try {
      const p = ports.split(/[\s,]+/).filter(Boolean).map(Number);
      const r = await api.scanCameras({ subnet: subnet.trim(), ports: p.length ? p : undefined,
        ...(password ? { username, password } : {}) });
      setFound(r.devices);
    } catch (e) {
      setErr(errorText(e));
    }
    setBusy(false);
  };
  return (
    <details className="field scan-box">
      <summary>Scan a subnet</summary>
      <div className="row">
        <input value={subnet} placeholder="10.20.7.0/24" onChange={(e) => setSubnet(e.target.value)} title="A private subnet, at most a /22" />
        <input value={ports} onChange={(e) => setPorts(e.target.value)} title="ONVIF ports to try" style={{ maxWidth: 140 }} />
        <button className="ghost small" disabled={busy || !subnet.trim()} onClick={scan}>{busy ? "Scanning…" : "Scan"}</button>
      </div>
      <span className="small">Asks every address for ONVIF (works over a VPN, where discovery doesn't). Type the camera password below first to see model names on cameras that hide them.</span>
      {err && <p className="error small">{err}</p>}
      {found && !found.length && <p className="muted small">No ONVIF devices answered.</p>}
      {found && found.length > 0 && (
        <ul className="scan-results">
          {found.map((d) => (
            <li key={`${d.address}:${d.port}`} className="row small">
              <span><strong>{d.address}</strong>:{d.port}</span>
              <span className="muted">{[d.manufacturer, d.model, d.firmware && `fw ${d.firmware}`].filter(Boolean).join(" · ")
                || (d.auth_failed ? "password refused" : d.needs_auth ? "needs credentials" : "ONVIF device")}</span>
              {d.camera_id ? <span className="muted">added as {d.camera_id}</span>
                : <button className="ghost small" onClick={() => onUse(d)}>Use</button>}
            </li>
          ))}
        </ul>
      )}
    </details>
  );
}
