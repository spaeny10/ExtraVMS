/* Settings › Cameras › Edit › Camera › Traffic (plate reading): a Milesight traffic camera's own plate reader and its
   push to this server (backend traffic.py camera_state / configure, over Milesight's web API). Shown only for a camera
   that answers as a traffic camera; anything else renders nothing. "Set up plate reads" turns plate reading on and points
   a free push slot at this server (admin; a 409 says why it can't: no licence, every slot in use); "Stop push" empties
   our slot. Without an SD card the camera can't resend, so reads pushed while this server is down are lost: an amber
   warning says so. Pure logic: traffic.ts. */
import { useEffect, useState } from "react";
import { api, type Camera } from "./api";
import { type CameraTraffic, isSilent, ourSlot, pushText, slotText } from "./traffic";
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
  </>;
}
