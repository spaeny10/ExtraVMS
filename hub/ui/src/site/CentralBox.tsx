/**
 * Site → Servers: central recording, read-only. Hub administrators allocate an instance on a datacenter host from the
 * Hosts page (storage, camera limit) and change it there. Everyone who sees this card (the Site's admins, hub
 * administrators) sees its state, storage, cameras against the limit, the camera addresses in use and the Peplink
 * settings sheet to apply to the BR1 (SpeedFusion VPN, or port forwards locked to the datacenter's IP). Once enrolled the
 * instance is an ordinary server of the Site (the cards below): its admins add cameras on its console, and the hub
 * opens each camera's address on the instance's firewall by itself. Hub administrators also see the host and GPU and a
 * link to manage it. No instance: hidden for customers, a one-line hint for hub administrators.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { type CentralInstance, type LocationCentral, type Site, api, fmtTime } from "../api";
import {
  PHASE_LABEL, PHASE_STEPS, addressParts, camerasText, cameraNetworkOf, fmtGB, forwardCount, forwardRows, forwardTargets, lanGateway, overLimit,
  phaseStep, quotaText, resolvedLines, settling, shortGpu,
} from "../central";
import { go } from "../nav";

const HOSTS_HREF = "/hub/hosts";

export function CentralBox({ site, onChanged }: { site: Site; onChanged: () => void }) {
  const [data, setData] = useState<LocationCentral | null>(null);
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
  const manage = !!data.can_manage;
  if (!inst && !manage) return null;
  return (
    <div className="card central-box">
      <div className="row">
        <h3 style={{ margin: 0 }}>Central recording</h3>
        <span className="muted small">this Site's cameras recorded in the datacenter</span>
        <span className="spacer" />
        {manage && inst && <a className="small" href={HOSTS_HREF} onClick={go(HOSTS_HREF)}>Manage on the Hosts page</a>}
      </div>
      {!inst && <p className="muted small" style={{ marginBottom: 0 }}>No central recording; allocate one from the <a href={HOSTS_HREF} onClick={go(HOSTS_HREF)}>Hosts page</a>.</p>}
      {inst && <Instance site={site} ci={inst} admin={manage} />}
    </div>
  );
}

function Steps({ phase }: { phase: string }) {
  const at = phaseStep(phase);
  const names = ["Provisioning", "Waiting to enroll", "Running"];
  return (
    <ol className="central-steps small">{PHASE_STEPS.map((p, i) => <li key={p} className={at >= i ? (at === i && p !== "running" ? "now" : "done") : ""}>{names[i]}</li>)}</ol>
  );
}

function Instance({ site, ci, admin }: { site: Site; ci: CentralInstance; admin: boolean }) {
  const site_nets = cameraNetworkOf(ci);
  const parts = addressParts(ci);
  const resolved = resolvedLines([...site_nets.hosts, ...(ci.camera_network?.auto?.hosts ?? [])], ci.camera_network?.resolved);
  const over = overLimit(ci.camera_count, ci.camera_limit);
  return (
    <>
      {ci.state === "failed" || ci.state === "deleting" ? <p className={ci.state === "failed" ? "bad" : "muted"}>{PHASE_LABEL[ci.phase] ?? ci.phase}{admin && ci.last_error ? `: ${ci.last_error}` : ""}</p> : <Steps phase={ci.phase} />}
      <div className="stats-grid small">
        <div><span className="muted">Server</span> {ci.name}{ci.server_id ? (ci.server_online ? " · online" : " · offline") : " · not enrolled yet"}</div>
        <div><span className="muted">Connection</span> {ci.mode === "vpn" ? `SpeedFusion VPN · ${ci.subnet ?? "—"}` : "Port forwarding"}</div>
        <div><span className="muted">Storage</span> {quotaText(ci.used_gb, ci.quota_gb)}</div>
        <div title={over ? "More cameras than the limit (it was lowered): they keep recording, new ones are refused" : undefined}>
          <span className="muted">Cameras</span> {camerasText(ci.camera_count, ci.camera_limit)}{over ? " (over the limit)" : ""}</div>
        {ci.site_number != null && <div><span className="muted">Site number</span> {ci.site_number}</div>}
        {admin && <div><span className="muted">Host</span> {ci.host_name ?? ci.host_id}{ci.host_online === false ? " (offline)" : ""}{ci.gpu != null ? ` · GPU ${ci.gpu}${ci.gpu_name ? ` ${shortGpu(ci.gpu_name)}` : ""}` : " · no GPU"}</div>}
        <div><span className="muted">Added</span> {fmtTime(ci.created_at)}</div>
      </div>
      <p className="small central-cameras" title={resolved.join("\n") || undefined}>
        Cameras reachable at: {parts.site.length ? parts.site.join(" · ") : "no Site network"}
        {parts.cameras.length > 0 && <> · opened for its cameras: {parts.cameras.join(" · ")}</>}
      </p>
      <p className="muted small">Add cameras on the instance's console (Open server console on its card below), like on any server: their addresses open on the datacenter firewall by themselves.
        {ci.camera_limit != null ? ` Up to ${ci.camera_limit} camera${ci.camera_limit === 1 ? "" : "s"}; ask Axiom Vision for more.` : ""}</p>
      {admin && ci.camera_network?.pending && <p className="small bad">Camera addresses not on the host yet{ci.camera_network.sync_error ? `: ${ci.camera_network.sync_error}` : " (sent within a minute)"}</p>}
      {admin && ci.camera_network?.auto_on === false && <p className="muted small">Automatic camera addresses start once this instance's Site networks are saved on the Hosts page.</p>}
      {admin && <p className="muted small">Storage {fmtGB(ci.quota_gb)}, camera limit, Site networks and removal: <a href={HOSTS_HREF} onClick={go(HOSTS_HREF)}>Hosts page</a>.</p>}
      <PeplinkSheet ci={ci} siteName={site.name} />
    </>
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
          <tr><th>On the instance</th><td>Add each camera by its LAN address (e.g. <code>{p.lan_gateway ? p.lan_gateway.replace(/\.1$/, ".11") : "10.20.7.11"}</code>) with its username and password, like on any server; it must be inside this Site's camera network</td></tr>
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
        public RTSP port and public ONVIF port from this table (camera 1: {rows[0]?.rtsp ?? p.rtsp_base + 1} / {rows[0]?.onvif ?? p.onvif_base + 1}). The instance rewrites the addresses the camera hands back to these, and the router's address opens on the datacenter firewall by itself.</p>
    </details>
  );
}
