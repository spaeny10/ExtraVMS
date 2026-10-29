/* Settings › Cameras › Edit › Camera › Traffic (plate reading): a Milesight traffic camera's own plate reader and its
   push to this server (backend traffic.py camera_state / configure, over Milesight's web API). Shown only for a camera
   that answers as a traffic camera; anything else renders nothing. "Set up plate reads" turns plate reading on and points
   a free push slot at this server (admin; a 409 says why it can't: no licence, every slot in use); "Stop push" empties
   our slot. Without an SD card the camera can't resend, so reads pushed while this server is down are lost: an amber
   warning says so. Plate alerts: a plate circling (passing again and again), a plate never seen before and a regular at
   an unusual time each raise an alarm that reaches the hub and the SOC (backend traffic.check_patterns); new and unusual
   only during SOC hours unless "Any time" (admin; saved at once). Pure logic: traffic.ts. */
import { useEffect, useState } from "react";
import { api, type Camera } from "./api";
import { type CameraTraffic, type CameraTrafficServer, type TrafficAlerts, isSilent, ourSlot, pushText, slotText } from "./traffic";
import { confirmDialog, errorText, toast } from "./ui";

export function TrafficSetupSection({ camera }: { camera: Camera }) {
  const [t, setT] = useState<CameraTraffic | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const read = () => api.cameraTraffic(camera.id).then(setT).catch(() => setT(null));   // a viewer's 403, an old server: nothing shown
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { let alive = true; api.cameraTraffic(camera.id).then((x) => { if (alive) setT(x); }).catch(() => {}); return () => { alive = false; }; }, [camera.id]);
  if (!t?.supported) return null;

  const c = t.camera, s = t.server;
  const mine = ourSlot(c.slots);
  const pushing = s.enabled && !!mine?.enabled;
  const silent = isSilent(s, s.silent_after_s);
  const change = async (enabled: boolean) => {
    if (!enabled && !(await confirmDialog(`Stop ${camera.name}'s plate reads?`, {
      message: "Its push slot is emptied: no more reads arrive here. Plate reading stays on on the camera.", confirmLabel: "Stop push", danger: true,
    }))) return;
    setBusy(true); setErr("");
    try {
      setT(await api.setCameraTraffic(camera.id, { enabled }));
      toast.success(`${camera.name}: ${enabled ? "plate reads now push to this server" : "push stopped"}`);
    } catch (e) { setErr(errorText(e)); }   // 409: no licence or no free slot, in the server's words; 502: the camera didn't answer
    setBusy(false);
  };

  return <>
    <h3 className="spaced">Traffic (plate reading)</h3>
    <div className="device-card">
      <p className="small">
        <strong>{c.model ?? "Traffic camera"}</strong>{c.firmware && <span className="muted"> · firmware {c.firmware}</span>}
        {" · "}{c.licensed ? <span>plate-reading licence active{c.country ? ` (${c.country})` : ""}</span> : <span className="error">no plate-reading licence</span>}
      </p>
      <p className="small">Plate reading on the camera: <strong>{c.lpr_enabled ? "on" : "off"}</strong></p>
      <p className="small">{slotText(c.slots)}</p>
      {s.enabled && (
        <p className="small">
          <span className={`dot ${silent ? "bad" : "ok"}`} /> {pushText(s)}
          {s.last_read ? <span className="muted"> · last read {new Date(s.last_read * 1000).toLocaleTimeString()}</span> : null}
          {silent && <span className="warn-text"> · nothing for over {Math.round(s.silent_after_s / 60)} min: check the camera's network</span>}
        </p>
      )}
      {c.sd_card === false && (
        <p className="small warn-text">⚠ No SD card: reads pushed while this server is down are lost (the camera resends only from a card).</p>
      )}
      <div className="row">
        {pushing
          ? <button className="ghost" disabled={busy} onClick={() => change(false)}>{busy ? "…" : "Stop push"}</button>
          : <button disabled={busy || !c.licensed} onClick={() => change(true)}>{busy ? "Setting up…" : "Set up plate reads"}</button>}
        <button className="ghost small" disabled={busy} onClick={read}>Read again</button>
        <span className="muted small">Changes the camera at once (admin).</span>
      </div>
      {err && <p className="small error">{err}</p>}
    </div>
    {s.alerts && <PlateAlerts camera={camera} server={s} onSaved={(server) => setT({ ...t, server })} />}
  </>;
}

const ALERTS: [keyof Omit<TrafficAlerts, "when">, string, (s: CameraTrafficServer) => string][] = [
  ["circling", "Circling", (s) => `The same plate passes ${s.circling?.passes ?? 3} times within ${s.circling?.minutes ?? 30} minutes (medium priority)`],
  ["new", "New plate", (s) => `A plate never seen here before, once the camera has ${s.learn_days ?? 14} days of plates (medium)`],
  ["unusual_time", "Regular at an unusual time", () => "A plate seen often, mostly at set times, now hours away from any of them (low)"],
];

function PlateAlerts({ camera, server, onSaved }: { camera: Camera; server: CameraTrafficServer; onSaved: (s: CameraTrafficServer) => void }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const a = server.alerts!;
  const save = async (change: Partial<TrafficAlerts>) => {
    setBusy(true); setErr("");
    try { onSaved(await api.setTrafficAlerts(camera.id, change)); } catch (e) { setErr(errorText(e)); }   // 403: admin only
    setBusy(false);
  };
  return <>
    <h3 className="spaced">Plate alerts</h3>
    <div className="device-card">
      <p className="small muted">Each raises an alarm with the plate's history, on the hub and in the SOC queue like a deny-list hit. Allowed plates never do.</p>
      {ALERTS.map(([k, label, hint]) => (
        <label key={k} className="row small" title={hint(server)}>
          <input type="checkbox" checked={a[k]} disabled={busy} onChange={(e) => save({ [k]: e.target.checked })} />
          <span><strong>{label}</strong> <span className="muted">· {hint(server)}</span></span>
        </label>
      ))}
      <label className="row small">
        New plates and unusual times:
        <select value={a.when} disabled={busy || !(a.new || a.unusual_time)} onChange={(e) => save({ when: e.target.value as TrafficAlerts["when"] })}>
          <option value="armed">Only during SOC hours</option>
          <option value="always">Any time</option>
        </select>
      </label>
      {err && <p className="small error">{err}</p>}
    </div>
  </>;
}
