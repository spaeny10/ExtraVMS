/**
 * Site → Servers: central recording. Hub administrators add an instance on a datacenter host ("Add central
 * recording"), watch it come up (provisioning → waiting to enroll → running), change its quota or remove it. The
 * Site's admins see its state and the Peplink settings sheet they apply to the BR1 (SpeedFusion VPN, or port forwards
 * locked to the datacenter's IP). Hub administrators also set the instance's camera addresses (LAN / VPN subnets, public
 * IPs, dynamic DNS names: its firewall allow-list), so one Site can mix cameras on a routed LAN with remote ones behind
 * port forwards. Once enrolled the instance is an ordinary server of the Site (the cards below).
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { confirmDialog, promptDialog, toast } from "@site/ui";
import { type CentralInstance, type CentralMode, type LocationCentral, type Site, api, fmtTime } from "../api";
import {
  CAMERA_KINDS, type CameraKind, type CameraLists, MAX_CAMERA_ENTRIES, PHASE_LABEL, PHASE_STEPS, cameraEntryError, cameraNetworkError,
  cameraNetworkOf, cameraNetworkSummary, centralFormError, cleanCameraNetwork, defaultSubnet, fmtGB, forwardCount, forwardRows,
  forwardTargets, lanGateway, nextSiteNumber, phaseStep, quotaText, resolvedLines, settling, shortGpu,
} from "../central";

export function CentralBox({ site, onChanged }: { site: Site; onChanged: () => void }) {
  const [data, setData] = useState<LocationCentral | null>(null);
  const [adding, setAdding] = useState(false);
  const load = useCallback(() => api.locationCentral(site.id).then(setData).catch(() => setData(null)), [site.id]);
  useEffect(() => { load(); }, [load]);
  const busy = !!data?.instances.some(settling);
  // while an instance is coming up (or going away) the page follows it closely
  useEffect(() => { const t = setInterval(load, busy ? 3000 : 15000); return () => clearInterval(t); }, [load, busy]);
  // it just finished (enrolled, or removed): the Site's server list changed too
  const wasBusy = useRef(false);
  useEffect(() => { if (wasBusy.current && !busy) onChanged(); wasBusy.current = busy; }, [busy, onChanged]);
  if (!data) return null;
  const inst = data.instances[0];
  if (!inst && !data.can_provision) return null;
  return (
    <div className="card central-box">
      <div className="row">
        <h3 style={{ margin: 0 }}>Central recording</h3>
        <span className="muted small">this Site's cameras recorded in the datacenter</span>
        <span className="spacer" />
        {!inst && data.can_provision && !adding && <button className="small" onClick={() => setAdding(true)}>Add central recording</button>}
      </div>
      {!inst && !adding && <p className="muted small" style={{ marginBottom: 0 }}>None. A hub administrator can place an instance for this Site on a datacenter host.</p>}
      {!inst && adding && <AddCentral site={site} data={data} onCancel={() => setAdding(false)} onDone={() => { setAdding(false); load(); }} />}
      {inst && <Instance site={site} ci={inst} admin={data.can_provision} onChanged={() => { load(); onChanged(); }} />}
    </div>
  );
}

function AddCentral({ site, data, onCancel, onDone }: { site: Site; data: LocationCentral; onCancel: () => void; onDone: () => void }) {
  const [mode, setMode] = useState<CentralMode>("vpn");
  const [host, setHost] = useState("");
  const [n, setN] = useState<number | null>(null);
  const [subnet, setSubnet] = useState("");
  const [publicIp, setPublicIp] = useState("");
  const [quota, setQuota] = useState("2000");
  const [name, setName] = useState("Central");
  const [busy, setBusy] = useState(false);
  // prefill with the site number the hub will most likely assign (it decides; an untouched field is left to it)
  useEffect(() => { api.hubCentral().then((all) => { const next = nextSiteNumber(all.map((c) => c.site_number)); setN(next); if (next) setSubnet(defaultSubnet(next)); }).catch(() => {}); }, []);
  const err = centralFormError({ mode, public_ip: publicIp, subnet, quota_gb: quota });
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      const edited = subnet.trim() && !(n && subnet.trim() === defaultSubnet(n));
      await api.provisionCentral(site.id, { mode, host_id: host || null, quota_gb: Number(quota), name: name.trim() || null,
        subnet: mode === "vpn" && edited ? subnet.trim() : null, public_ip: mode === "forward" ? publicIp.trim() : null });
      toast.success("Provisioning started");
      onDone();
    } catch (er) { toast.error(er); } finally { setBusy(false); }
  };
  return (
    <form className="central-form" onSubmit={submit}>
      <label className="field"><span>Host</span>
        <select value={host} onChange={(e) => setHost(e.target.value)}>
          <option value="">Automatic (the online host with the most room)</option>
          {data.hosts.map((h) => <option key={h.id} value={h.id} disabled={!h.online}>{h.name}{h.online ? "" : " (offline)"} · {h.instances} instance{h.instances === 1 ? "" : "s"}{(h.capacity?.gpus ?? []).length ? ` · ${(h.capacity?.gpus ?? []).map((g) => shortGpu(g.name)).join(" + ")}` : ""}</option>)}
        </select>
      </label>
      <div className="field"><span>Connection to the site</span>
        <div className="segmented small-seg">
          <button type="button" className={mode === "vpn" ? "active" : ""} onClick={() => setMode("vpn")}>SpeedFusion VPN</button>
          <button type="button" className={mode === "forward" ? "active" : ""} onClick={() => setMode("forward")}>Port forwarding</button>
        </div>
        <span className="muted small">{mode === "vpn" ? "Encrypted, no camera port exposed. The BR1's LAN gets its own subnet." : "No VPN to run, but video and ONVIF travel unencrypted; forwards are locked to the datacenter's IP."}</span>
      </div>
      {mode === "vpn"
        ? <label className="field"><span>Camera subnet (the BR1's LAN)</span><input value={subnet} placeholder="10.20.7.0/24" onChange={(e) => setSubnet(e.target.value)} /></label>
        : <label className="field"><span>Site's public IP (the BR1's WAN address)</span><input value={publicIp} placeholder="e.g. 93.184.216.34" onChange={(e) => setPublicIp(e.target.value)} /></label>}
      <label className="field"><span>Storage quota (GB)</span><input inputMode="numeric" value={quota} onChange={(e) => setQuota(e.target.value.replace(/[^0-9]/g, ""))} /></label>
      <label className="field"><span>Server name in this Site</span><input value={name} maxLength={120} onChange={(e) => setName(e.target.value)} /></label>
      <div className="row">
        <button type="submit" disabled={busy || !!err}>Provision</button>
        <button type="button" className="ghost" onClick={onCancel}>Cancel</button>
        {err && <span className="muted small">{err}</span>}
      </div>
    </form>
  );
}

function Steps({ phase }: { phase: string }) {
  const at = phaseStep(phase);
  const names = ["Provisioning", "Waiting to enroll", "Running"];
  return (
    <ol className="central-steps small">{PHASE_STEPS.map((p, i) => <li key={p} className={at >= i ? (at === i && p !== "running" ? "now" : "done") : ""}>{names[i]}</li>)}</ol>
  );
}

function Instance({ site, ci, admin, onChanged }: { site: Site; ci: CentralInstance; admin: boolean; onChanged: () => void }) {
  const [purge, setPurge] = useState(false);
  const [editing, setEditing] = useState(false);
  const cams = cameraNetworkOf(ci);
  const resolved = resolvedLines(cams.hosts, ci.camera_network?.resolved);
  const remove = async () => {
    const msg = purge ? "The instance and ALL its recordings are deleted on the host. This cannot be undone." : "The instance is deleted on the host; its recordings and database stay there. Its server is retired in this Site.";
    if (!(await confirmDialog(`Remove central recording for ${site.name}?`, { message: msg, confirmLabel: "Remove", danger: true }))) return;
    try { await api.removeCentral(site.id, ci.id, { purge }); toast.success("Central recording removed"); onChanged(); }
    catch (e) {
      toast.error(e);
      if (await confirmDialog("Remove it anyway?", { message: "The hub forgets the instance even though its host could not delete it (it may keep running there until removed by hand).", confirmLabel: "Remove anyway", danger: true })) {
        try { await api.removeCentral(site.id, ci.id, { purge, force: true }); toast.success("Removed at the hub"); onChanged(); } catch (e2) { toast.error(e2); }
      }
    }
  };
  const quota = async () => {
    const v = await promptDialog("Storage quota (GB)", { initial: String(ci.quota_gb), label: "GB" });
    const q = Number((v ?? "").trim());
    if (!v || !Number.isInteger(q) || q < 1) return;
    try { await api.setCentralQuota(site.id, ci.id, q); toast.success(`Quota set to ${fmtGB(q)}`); onChanged(); } catch (e) { toast.error(e); }
  };
  return (
    <>
      {ci.state === "failed" || ci.state === "deleting" ? <p className={ci.state === "failed" ? "bad" : "muted"}>{PHASE_LABEL[ci.phase] ?? ci.phase}{ci.last_error ? `: ${ci.last_error}` : ""}</p> : <Steps phase={ci.phase} />}
      <div className="stats-grid small">
        <div><span className="muted">Server</span> {ci.name}{ci.server_id ? (ci.server_online ? " · online" : " · offline") : " · not enrolled yet"}</div>
        <div><span className="muted">Connection</span> {ci.mode === "vpn" ? `SpeedFusion VPN · ${ci.subnet ?? "—"}` : "Port forwarding"}</div>
        <div><span className="muted">Storage</span> {quotaText(ci.used_gb, ci.quota_gb)}</div>
        {ci.site_number != null && <div><span className="muted">Site number</span> {ci.site_number}</div>}
        {admin && <div><span className="muted">Host</span> {ci.host_name ?? ci.host_id}{ci.host_online === false ? " (offline)" : ""}{ci.gpu != null ? ` · GPU ${ci.gpu}${ci.gpu_name ? ` ${shortGpu(ci.gpu_name)}` : ""}` : " · no GPU"}</div>}
        <div><span className="muted">Added</span> {fmtTime(ci.created_at)}</div>
      </div>
      <p className="small central-cameras" title={resolved.join("\n") || undefined}>{cameraNetworkSummary(cams)}</p>
      {admin && (
        <div className="row server-actions">
          <button className="ghost small" onClick={quota}>Change quota…</button>
          <button className="ghost small" onClick={() => setEditing(true)}>Camera addresses…</button>
          <label className="small row"><input type="checkbox" checked={purge} onChange={(e) => setPurge(e.target.checked)} /> delete recordings too</label>
          <button className="ghost small" onClick={remove}>Remove central recording</button>
        </div>
      )}
      <PeplinkSheet ci={ci} siteName={site.name} />
      {editing && <CameraAddresses site={site} ci={ci} onClose={() => setEditing(false)} onSaved={() => { setEditing(false); onChanged(); }} />}
    </>
  );
}

const KIND_INFO: Record<CameraKind, { title: string; hint: string; placeholder: string; add: string }> = {
  subnets: { title: "LAN / VPN subnets", hint: "Camera networks the datacenter host routes to (a LAN, a SpeedFusion VPN). Any protocol.", placeholder: "192.168.105.0/24", add: "Add subnet" },
  public_ips: { title: "Public IPs", hint: "Site routers with port forwards to their cameras. TCP only.", placeholder: "203.0.113.7", add: "Add public IP" },
  hosts: { title: "Host names", hint: "Dynamic DNS names of such routers, looked up again every 10 minutes. TCP only.", placeholder: "cam1.example.net", add: "Add host name" },
};

/** The instance's camera allow-list editor: three lists with add/remove rows, checked as you type. */
function CameraAddresses({ site, ci, onClose, onSaved }: { site: Site; ci: CentralInstance; onClose: () => void; onSaved: () => void }) {
  const [lists, setLists] = useState<CameraLists>(() => cameraNetworkOf(ci));
  const [busy, setBusy] = useState(false);
  const edit = (k: CameraKind, f: (xs: string[]) => string[]) => setLists((l) => ({ ...l, [k]: f(l[k]) }));
  const err = cameraNetworkError(lists);
  const clean = cleanCameraNetwork(lists);
  const total = clean.subnets.length + clean.public_ips.length + clean.hosts.length;
  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try { await api.setCentralCameras(site.id, ci.id, clean); toast.success("Camera addresses saved"); onSaved(); }
    catch (er) { toast.error(er); } finally { setBusy(false); }
  };
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <form className="modal camera-addresses" onClick={(e) => e.stopPropagation()} onSubmit={save} style={{ maxWidth: 560 }}>
        <header className="modal-head"><h2>{site.name} · camera addresses</h2><button type="button" className="ghost" onClick={onClose} aria-label="Close">✕</button></header>
        <p className="muted small">The instance may reach exactly these addresses (its firewall on the host); everything else stays blocked. Cameras behind port forwards still get their outside address and ports set on the instance, camera by camera.</p>
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
        {total === 0 && <p className="muted small">With no addresses the instance can reach no camera.</p>}
        <div className="row">
          <button type="submit" disabled={busy || !!err}>Save</button>
          <button type="button" className="ghost" onClick={onClose}>Cancel</button>
          {err && <span className="muted small">{err}</span>}
        </div>
      </form>
    </div>
  );
}

/** What to set on the site's Peplink BR1, for the instance's mode. */
export function PeplinkSheet({ ci, siteName }: { ci: CentralInstance; siteName: string }) {
  const targets = forwardTargets(ci.peplink);
  return (
    <>
      {ci.peplink.mode === "vpn" && <VpnSheet ci={ci} siteName={siteName} />}
      {(ci.peplink.mode === "forward" || targets.length > 0) && <ForwardSheet ci={ci} targets={targets} />}
    </>
  );
}

function VpnSheet({ ci, siteName }: { ci: CentralInstance; siteName: string }) {
  const p = ci.peplink;
  return (
    <details className="peplink" open={ci.phase !== "running"}>
      <summary><strong>Peplink settings</strong> <span className="muted small">SpeedFusion VPN</span></summary>
      <table className="hub-table small">
        <tbody>
          <tr><th>LAN subnet</th><td><code>{p.subnet ?? "—"}</code> · BR1 LAN address <code>{p.lan_gateway ?? lanGateway(p.subnet) ?? "—"}</code>. Not the default 192.168.50.0/24: every site needs its own.</td></tr>
          <tr><th>SpeedFusion peer</th><td>{p.fusionhub ? <code>{p.fusionhub}</code> : <span className="muted">ask the hub administrator (the host's FusionHub address is not set)</span>} · profile named <code>{siteName}</code>, managed in InControl 2</td></tr>
          <tr><th>WAN Smoothing</th><td><strong>Off</strong>: it duplicates every packet and doubles the cellular data</td></tr>
          <tr><th>Firewall</th><td>Only the SpeedFusion tunnel may reach the camera LAN; nothing forwarded from the internet</td></tr>
          <tr><th>Cameras</th><td>Fixed addresses in <code>{p.subnet ?? "the LAN subnet"}</code>, default passwords changed, NTP server = the BR1 (<code>{p.lan_gateway ?? "its LAN address"}</code>)</td></tr>
          <tr><th>On the instance</th><td>Add each camera by its LAN address (e.g. <code>{p.lan_gateway ? p.lan_gateway.replace(/\.1$/, ".11") : "10.20.7.11"}</code>) with its username and password, like on any server</td></tr>
        </tbody>
      </table>
    </details>
  );
}

/** Port forwards on each router the instance reaches by public IP or DNS name. */
function ForwardSheet({ ci, targets }: { ci: CentralInstance; targets: string[] }) {
  const p = ci.peplink;
  const names = targets.length > 1 ? [] : (ci.cameras ?? []).map((c) => c.name);   // several routers: numbered per router
  const rows = forwardRows(forwardCount(names.length), p.rtsp_base, p.onvif_base, names);
  return (
    <details className="peplink" open={ci.phase !== "running"}>
      <summary><strong>Peplink settings</strong> <span className="muted small">port forwarding{targets.length ? ` · ${targets.join(" · ")}` : ""}</span></summary>
      {targets.length > 1 && <p className="small">This applies to each router reached by port forwards: {targets.map((t, i) => <span key={t}>{i ? " · " : ""}<code>{t}</code></span>)}. Number the cameras behind each router from 1.</p>}
      <p className="small">On {targets.length > 1 ? "each" : "the"} BR1, two forwards per camera from the WAN ({targets.length === 1 ? <code>{targets[0]}</code> : "its public IP or DNS name"}) to the camera's LAN address,
        each with <strong>allowed source = {p.datacenter_ip ? <code>{p.datacenter_ip}</code> : <span className="muted">the datacenter IP (not set on the hub: HUB_DATACENTER_IP)</span>}</strong> only.
        Don't forward the camera web page (80/443) unless ONVIF uses it. Video, ONVIF and the camera login travel unencrypted: use RTSPS/HTTPS where the camera supports it.</p>
      <table className="hub-table small">
        <thead><tr><th>Camera</th><th>Outside RTSP port</th><th>→ camera</th><th>Outside ONVIF port</th><th>→ camera</th></tr></thead>
        <tbody>{rows.map((r) => (
          <tr key={r.camera}><td>{r.camera}{r.name ? ` · ${r.name}` : ""}</td><td><code>{r.rtsp}</code></td><td>554</td><td><code>{r.onvif}</code></td><td>80 (or its ONVIF port)</td></tr>))}
        </tbody>
      </table>
      <p className="small">On the instance, add each camera with its <em>LAN</em> address, username and password, and set its outside address: public host {targets.length === 1 ? <code>{targets[0]}</code> : "its router's public IP or DNS name"},
        public RTSP port and public ONVIF port from this table (camera 1: {rows[0]?.rtsp ?? p.rtsp_base + 1} / {rows[0]?.onvif ?? p.onvif_base + 1}). The instance rewrites the addresses the camera hands back to these.</p>
    </details>
  );
}
