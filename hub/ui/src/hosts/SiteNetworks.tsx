/**
 * Hosts page → a central instance → "Site networks…" (hub administrators): the networks an administrator opens on the
 * instance's firewall, its Site's LAN / VPN subnets and router addresses to open before any camera uses them. The public
 * addresses the instance's cameras use are opened by the hub on its own (hub central_cameras.py) and listed here
 * read-only. Saving sends the host the whole firewall.
 */
import { useState } from "react";
import { toast } from "@site/ui";
import { type CentralInstance, api } from "../api";
import {
  CAMERA_KINDS, type CameraKind, type CameraLists, MAX_CAMERA_ENTRIES, addressParts, cameraEntryError, cameraNetworkError, cameraNetworkOf,
  cleanCameraNetwork,
} from "../central";

const KIND_INFO: Record<CameraKind, { title: string; hint: string; placeholder: string; add: string }> = {
  subnets: { title: "LAN / VPN subnets", hint: "The Site's camera networks the host routes to (a LAN, a SpeedFusion VPN). Any protocol. A camera at a private address must be inside one.", placeholder: "192.168.105.0/24", add: "Add subnet" },
  public_ips: { title: "Public IPs", hint: "Site routers with port forwards to open before a camera uses them. TCP only.", placeholder: "203.0.113.7", add: "Add public IP" },
  hosts: { title: "Host names", hint: "Dynamic DNS names of such routers, looked up again every 10 minutes. TCP only.", placeholder: "cam1.example.net", add: "Add host name" },
};

export function SiteNetworks({ ci, onClose, onSaved }: { ci: CentralInstance; onClose: () => void; onSaved: () => void }) {
  const [lists, setLists] = useState<CameraLists>(() => cameraNetworkOf(ci));
  const [busy, setBusy] = useState(false);
  const edit = (k: CameraKind, f: (xs: string[]) => string[]) => setLists((l) => ({ ...l, [k]: f(l[k]) }));
  const err = cameraNetworkError(lists);
  const clean = cleanCameraNetwork(lists);
  const total = clean.subnets.length + clean.public_ips.length + clean.hosts.length;
  const fromCameras = addressParts(ci).cameras;
  const siteName = ci.location_name ?? ci.location_id;
  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try { await api.setCentralCameras(ci.location_id, ci.id, clean); toast.success("Site networks saved"); onSaved(); }
    catch (er) { toast.error(er); } finally { setBusy(false); }
  };
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <form className="modal camera-addresses central-dialog" onClick={(e) => e.stopPropagation()} onSubmit={save} style={{ maxWidth: 560 }}>
        <header className="modal-head"><h2>{siteName} · Site networks</h2><button type="button" className="ghost" onClick={onClose} aria-label="Close">✕</button></header>
        <p className="muted small">The instance may reach these networks plus the public addresses its cameras use (opened and closed automatically as cameras are added and removed); everything else stays blocked.</p>
        {CAMERA_KINDS.map((k) => (
          <div key={k} className="field">
            <span><strong>{KIND_INFO[k].title}</strong> <span className="muted small">{KIND_INFO[k].hint}</span></span>
            {lists[k].map((v, i) => {
              const bad = cameraEntryError(k, v);
              return (
                <div key={i} className="row">
                  <input value={v} placeholder={KIND_INFO[k].placeholder} aria-invalid={!!bad} aria-label={KIND_INFO[k].title}
                    onChange={(e) => edit(k, (xs) => xs.map((x, j) => (j === i ? e.target.value : x)))} />
                  <button type="button" className="ghost small" aria-label="Remove" onClick={() => edit(k, (xs) => xs.filter((_, j) => j !== i))}>✕</button>
                  {bad && <span className="bad small">{bad}</span>}
                </div>
              );
            })}
            <div><button type="button" className="ghost small" disabled={total >= MAX_CAMERA_ENTRIES} onClick={() => edit(k, (xs) => [...xs, ""])}>{KIND_INFO[k].add}</button></div>
          </div>
        ))}
        <div className="field">
          <span><strong>From its cameras</strong> <span className="muted small">automatic; not edited here</span></span>
          <span className="small">{fromCameras.length ? fromCameras.join(" · ") : <span className="muted">none</span>}</span>
          {ci.camera_network?.auto_on === false && <span className="muted small">This instance is from before automatic camera addresses: they start once these networks are saved.</span>}
        </div>
        {!clean.subnets.length && <p className="muted small">With no subnet, cameras can only be reached through their routers' public addresses (port forwarding).</p>}
        <div className="row">
          <button type="submit" disabled={busy || !!err}>Save</button>
          <button type="button" className="ghost" onClick={onClose}>Cancel</button>
          {err && <span className="muted small">{err}</span>}
        </div>
      </form>
    </div>
  );
}
