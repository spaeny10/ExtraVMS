/**
 * Site → Servers: central recording, folded into the instance's own server card (read-only). Hub administrators allocate
 * an instance on a datacenter host from the Hosts page (storage, camera limit) and change it there. The Site's admins
 * and hub administrators see, on the card: the connection, the camera limit, the camera addresses in use, the progress
 * while it is not running, and the Peplink settings sheet to apply to the BR1 (SpeedFusion VPN, or port forwards locked
 * to the datacenter's IP). Hub administrators also see the host and GPU, errors, and a link to the Hosts page. Everyone
 * else sees the card's "Datacenter (central recording)" tag and its storage. Until the instance's server enrolls there is
 * no server card: a placeholder card shows its progress instead.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { type CentralInstance, type LocationCentral, api } from "../api";
import {
  PHASE_LABEL, PHASE_STEPS, addCamerasHint, connectionText, datacenterLine, forwardCount, forwardRows, forwardTargets, lanGateway, limitText,
  overLimit, phaseStep, quotaText, reachableText, resolvedLines, settling,
} from "../central";
import { go } from "../nav";

const HOSTS_HREF = "/hub/hosts";

/**
 * The Site's central instances (GET /api/locations/{id}/central: the Site's admins and hub administrators; `on` false =
 * not asked). Polled every 15 s, every 3 s while one is coming up or going away; when that settles the Site's server
 * list changed too (`onSettled`).
 */
export function useCentral(siteId: string, on: boolean, onSettled: () => void): LocationCentral | null {
  const [data, setData] = useState<LocationCentral | null>(null);
  const load = useCallback(() => (on ? api.locationCentral(siteId).then(setData).catch(() => setData(null)) : setData(null)), [siteId, on]);
  useEffect(() => { load(); }, [load]);
  const busy = !!data?.instances.some(settling);
  useEffect(() => { if (!on) return; const t = setInterval(load, busy ? 3000 : 15000); return () => clearInterval(t); }, [load, on, busy]);
  const wasBusy = useRef(false);
  useEffect(() => { if (wasBusy.current && !busy) onSettled(); wasBusy.current = busy; }, [busy, onSettled]);
  return data;
}

/** Rows for the card's stats grid: the connection and the camera limit (the storage row is the card's own). */
export function CentralStats({ ci }: { ci: CentralInstance }) {
  const over = overLimit(ci.camera_count, ci.camera_limit);
  return (
    <>
      <div><span>Connection</span> {connectionText(ci)}</div>
      <div title={over ? "More cameras than the limit (it was lowered): they keep recording, new ones are refused" : undefined}>
        <span>Limit</span> {limitText(ci.camera_count, ci.camera_limit)}</div>
    </>
  );
}

function Steps({ phase }: { phase: string }) {
  const at = phaseStep(phase);
  const names = ["Provisioning", "Waiting to enroll", "Running"];
  return (
    <ol className="central-steps small">{PHASE_STEPS.map((p, i) => <li key={p} className={at >= i ? (at === i && p !== "running" ? "now" : "done") : ""}>{names[i]}</li>)}</ol>
  );
}

/** The card's central block: progress (when not running), camera addresses, the hint, the Peplink sheet. */
export function CentralDetails({ siteName, ci, hubAdmin, enrolled }: { siteName: string; ci: CentralInstance; hubAdmin: boolean; enrolled: boolean }) {
  const resolved = resolvedLines([...(ci.camera_network?.hosts ?? []), ...(ci.camera_network?.auto?.hosts ?? [])], ci.camera_network?.resolved);
  return (
    <div className="central-block small">
      {ci.state === "failed" || ci.state === "deleting"
        ? <p className={ci.state === "failed" ? "bad" : "muted"}>{PHASE_LABEL[ci.phase] ?? ci.phase}{hubAdmin && ci.last_error ? `: ${ci.last_error}` : ""}</p>
        : ci.phase !== "running" && <Steps phase={ci.phase} />}
      <p className="central-cameras" title={resolved.join("\n") || undefined}>{reachableText(ci)}</p>
      {enrolled && <p className="muted">{addCamerasHint(ci.camera_limit)}</p>}
      {hubAdmin && ci.camera_network?.pending && <p className="bad">Camera addresses not on the host yet{ci.camera_network.sync_error ? `: ${ci.camera_network.sync_error}` : " (sent within a minute)"}</p>}
      {hubAdmin && ci.camera_network?.auto_on === false && <p className="muted">Automatic camera addresses start once this instance's Site networks are saved on the Hosts page.</p>}
      <PeplinkSheet ci={ci} siteName={siteName} />
      {hubAdmin && <p className="central-manage"><a href={HOSTS_HREF} onClick={go(HOSTS_HREF)}>Manage on the Hosts page</a> <span className="muted">(storage, camera limit, Site networks, removal)</span></p>}
    </div>
  );
}

/** An instance whose server has not enrolled yet (or no longer has a card here): its progress in the server grid. */
export function CentralPlaceholder({ siteName, ci, hubAdmin }: { siteName: string; ci: CentralInstance; hubAdmin: boolean }) {
  const failed = ci.state === "failed";
  return (
    <div className={`site-card central-pending ${failed ? "failed" : ""}`}>
      <div className="head">
        <span className={`dot ${failed ? "bad" : ""}`} title={PHASE_LABEL[ci.phase] ?? ci.phase} />
        <strong>{ci.name}</strong>
        <span className="spacer" />
        <span className="muted small">{failed ? "failed" : ci.state === "deleting" ? "removing" : "not enrolled yet"}</span>
      </div>
      <div className="loc">{datacenterLine(ci, hubAdmin)}</div>
      <div className="stats">
        <div><span>Storage</span> {quotaText(ci.used_gb, ci.quota_gb)}</div>
        <CentralStats ci={ci} />
      </div>
      <CentralDetails siteName={siteName} ci={ci} hubAdmin={hubAdmin} enrolled={false} />
    </div>
  );
}

/** The sheet starts open while the instance is being set up (the router is configured meanwhile), closed otherwise. */
const settingUp = (ci: CentralInstance) => ci.phase === "provisioning" || ci.phase === "waiting_enroll";

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
    <details className="peplink" open={settingUp(ci)}>
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
    <details className="peplink" open={settingUp(ci)}>
      <summary><strong>Peplink settings</strong> <span className="muted small">port forwarding{targets.length ? ` · ${targets.join(" · ")}` : ""}</span></summary>
      {targets.length > 1 && <p className="small">This applies to each router reached by port forwards: {targets.map((t, i) => <span key={t}>{i ? " · " : ""}<code>{t}</code></span>)}. Number the cameras behind each router from 1.</p>}
      <p className="small">On {targets.length > 1 ? "each" : "the"} BR1, two forwards per camera from the WAN ({targets.length === 1 ? <code>{targets[0]}</code> : "its public IP or DNS name"}) to the camera's LAN address,
        each with <strong>allowed source = {p.datacenter_ip ? <code>{p.datacenter_ip}</code> : <span className="muted">the datacenter IP (not set on the hub: HUB_DATACENTER_IP)</span>}</strong> only.
        Don't forward the camera web page (80/443) unless ONVIF uses it. Video, ONVIF and the camera login travel unencrypted: use RTSPS/HTTPS where the camera supports it.</p>
      <div className="peplink-scroll">
        <table className="hub-table small">
          <thead><tr><th>Camera</th><th>Outside RTSP port</th><th>→ camera</th><th>Outside ONVIF port</th><th>→ camera</th></tr></thead>
          <tbody>{rows.map((r) => (
            <tr key={r.camera}><td>{r.camera}{r.name ? ` · ${r.name}` : ""}</td><td><code>{r.rtsp}</code></td><td>554</td><td><code>{r.onvif}</code></td><td>80 (or its ONVIF port)</td></tr>))}
          </tbody>
        </table>
      </div>
      <p className="small">On the instance, add each camera with its <em>LAN</em> address, username and password, and set its outside address: public host {targets.length === 1 ? <code>{targets[0]}</code> : "its router's public IP or DNS name"},
        public RTSP port and public ONVIF port from this table (camera 1: {rows[0]?.rtsp ?? p.rtsp_base + 1} / {rows[0]?.onvif ?? p.onvif_base + 1}). The instance rewrites the addresses the camera hands back to these, and the router's address opens on the datacenter firewall by itself.</p>
    </details>
  );
}
