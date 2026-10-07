/**
 * /hub/hosts (hub administrators): the datacenter machines that run central recording instances, each with its
 * axiom-host agent dialing the hub (online, CPU/RAM/GPU/disk, instances), and every Site's central instance across
 * customers. Adding a host shows its token once, with the command that starts the agent.
 */
import { useCallback, useEffect, useState } from "react";
import { confirmDialog, promptDialog, toast } from "@site/ui";
import { type CentralInstance, type Host, ago, api, fmtTime } from "../api";
import { PHASE_LABEL, capacityBars, fmtGB, quotaText } from "../central";
import { go, siteHref } from "../nav";

export function HostsPage() {
  const [hosts, setHosts] = useState<Host[] | null>(null);
  const [install, setInstall] = useState("");
  const [central, setCentral] = useState<CentralInstance[]>([]);
  const [issued, setIssued] = useState<{ name: string; token: string; install: string } | null>(null);
  const load = useCallback(() => {
    api.hosts().then((r) => { setHosts(r.hosts); setInstall(r.install); }).catch((e) => toast.error(e));
    api.hubCentral().then(setCentral).catch(() => setCentral([]));
  }, []);
  useEffect(() => { load(); const t = setInterval(load, 15000); return () => clearInterval(t); }, [load]);
  return (
    <>
      <h2>Hosts <span className="muted small">datacenter machines running central recording, one server instance per customer Site</span></h2>
      {issued && <TokenBox {...issued} onClose={() => setIssued(null)} />}
      <AddHost onAdded={(r, name) => { setIssued({ name, token: r.token, install: r.install }); load(); }} />
      {hosts === null ? <p className="muted">Loading…</p> : hosts.length === 0 ? (
        <p className="muted">No hosts yet. Add one, then run its agent with the token: <code>{install}</code></p>
      ) : (
        <div className="host-grid">{hosts.map((h) => <HostCard key={h.id} h={h} onChanged={load} onToken={(t) => setIssued({ name: h.name, ...t })} />)}</div>
      )}
      <div className="card">
        <h3 style={{ marginTop: 0 }}>Central instances <span className="muted small">{central.length} across every customer</span></h3>
        {central.length === 0 ? <p className="muted small">None yet: add one from a Site's Servers tab (Add central recording).</p> : (
          <table className="hub-table stack">
            <thead><tr><th>Site</th><th>Customer</th><th>Host</th><th>Network</th><th>GPU</th><th>Storage</th><th>State</th></tr></thead>
            <tbody>{central.map((c) => (
              <tr key={c.id}>
                <td className="lead"><a href={siteHref(c.location_id, "servers")} onClick={go(siteHref(c.location_id, "servers"))}>{c.location_name ?? c.location_id}</a>
                  {c.site_number != null && <span className="muted small"> · site #{c.site_number}</span>}</td>
                <td>{c.org_name ?? c.org_id}</td>
                <td>{c.host_name ?? c.host_id}{c.host_online === false ? <span className="muted small"> (offline)</span> : null}</td>
                <td className="small">{c.mode === "vpn" ? `VPN · ${c.subnet ?? "—"}` : `Port forward · ${c.public_ip ?? "—"}`}</td>
                <td className="small" data-label="GPU">{c.gpu != null ? `${c.gpu}${c.gpu_name ? ` · ${c.gpu_name.replace(/^NVIDIA\s+/i, "")}` : ""}` : "CPU"}</td>
                <td className="small" data-label="Storage">{quotaText(c.used_gb, c.quota_gb)}</td>
                <td className="small" title={c.last_error ?? undefined}>{PHASE_LABEL[c.phase] ?? c.phase}{c.state === "failed" && c.last_error ? `: ${c.last_error.slice(0, 80)}` : ""}</td>
              </tr>))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}

function AddHost({ onAdded }: { onAdded: (r: { token: string; install: string }, name: string) => void }) {
  const [name, setName] = useState("");
  const [fusionhub, setFusionhub] = useState("");
  const [notes, setNotes] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      const r = await api.addHost({ name: name.trim(), fusionhub: fusionhub.trim() || undefined, notes: notes.trim() || undefined });
      onAdded(r, r.host.name); setName(""); setFusionhub(""); setNotes("");
    } catch (err) { toast.error(err); } finally { setBusy(false); }
  };
  return (
    <details className="card">
      <summary><strong>Add host</strong> <span className="muted small">Ubuntu + Docker + NVIDIA Container Toolkit, then the axiom-host agent</span></summary>
      <form onSubmit={submit} style={{ marginTop: 8 }}>
        <label className="field"><span>Name</span><input value={name} maxLength={120} placeholder="G481 rack 4" onChange={(e) => setName(e.target.value)} /></label>
        <label className="field"><span>FusionHub address</span><input value={fusionhub} maxLength={200} placeholder="public IP of the SpeedFusion peer for VPN-mode sites" onChange={(e) => setFusionhub(e.target.value)} /></label>
        <label className="field"><span>Notes</span><textarea value={notes} rows={2} maxLength={2000} onChange={(e) => setNotes(e.target.value)} /></label>
        <button type="submit" disabled={busy || !name.trim()}>Add host</button>
      </form>
    </details>
  );
}

/** The host token, shown once (the hub keeps only a hash), and how to start the agent with it. */
function TokenBox({ name, token, install, onClose }: { name: string; token: string; install: string; onClose: () => void }) {
  const copy = (t: string) => navigator.clipboard?.writeText(t).then(() => toast.success("Copied"), () => toast.error("Copy failed: select and copy it by hand"));
  return (
    <div className="card token-box">
      <div className="row"><h3 style={{ margin: 0 }}>Token for {name}</h3><span className="spacer" /><button className="ghost small" onClick={onClose}>Done</button></div>
      <p className="small">Shown only now: the hub keeps a hash. On the host, save it as the token file (readable by root only), then start the agent.</p>
      <ol className="small">
        <li>Token: <code className="token">{token}</code> <button className="ghost small" onClick={() => copy(token)}>Copy</button><br />
          <span className="muted">e.g. <code>sudo install -m 600 /dev/stdin /etc/axiom/host-token</code>, paste, press Enter, then Ctrl-D</span></li>
        <li>Run: <code>{install}</code> <button className="ghost small" onClick={() => copy(install)}>Copy</button></li>
      </ol>
    </div>
  );
}

function Bars({ h }: { h: Host }) {
  const bars = capacityBars(h.capacity);
  if (!bars.length) return <p className="muted small">No capacity reported yet.</p>;
  return (
    <div className="cap-bars">{bars.map((b) => (
      <div key={b.label} className="cap-row small">
        <span className="cap-label">{b.label}</span>
        <span className={`cap-bar ${b.pct >= 90 ? "hot" : b.pct >= 75 ? "warm" : ""}`}><span style={{ width: `${b.pct}%` }} /></span>
        <span className="muted">{b.text}</span>
      </div>))}
    </div>
  );
}

function HostCard({ h, onChanged, onToken }: { h: Host; onChanged: () => void; onToken: (t: { token: string; install: string }) => void }) {
  const act = (f: () => Promise<unknown>, done?: string) => async () => { try { await f(); if (done) toast.success(done); onChanged(); } catch (e) { toast.error(e); } };
  const edit = async (field: "name" | "fusionhub" | "notes", title: string) => {
    const v = await promptDialog(title, { initial: (h[field] as string | null) ?? "", label: title });
    if (v == null || (field === "name" && !v.trim())) return;
    await act(() => api.updateHost(h.id, { [field]: v.trim() || null }))();
  };
  return (
    <div className={`site-card ${h.online ? "" : "offline"}`}>
      <div className="head">
        <span className={`dot ${h.online ? "ok" : "bad"}`} title={h.online ? "Online" : `Offline · last seen ${ago(h.last_seen_at)}`} />
        <strong>{h.name}</strong>
        <span className="spacer" />
        <span className="muted small">{h.online ? "online" : h.last_seen_at ? `offline · ${ago(h.last_seen_at)}` : "never connected"}</span>
      </div>
      {h.offline_since && <div className="alerts">⚠ offline since {fmtTime(h.offline_since)}</div>}
      <div className="stats small">
        <div><span>Machine</span> {h.hostname ?? "—"}{h.agent_ip ? ` · ${h.agent_ip}` : ""}</div>
        <div><span>Agent</span> {h.version ?? "—"}</div>
        <div><span>Instances</span> {h.instances}{h.quota_gb ? ` · ${fmtGB(h.quota_gb)} of quota` : ""}</div>
        <div><span>FusionHub</span> {h.fusionhub ?? "—"}</div>
      </div>
      <Bars h={h} />
      {h.notes && <p className="muted small" style={{ whiteSpace: "pre-wrap" }}>{h.notes}</p>}
      <div className="row server-actions">
        <button className="ghost small" onClick={() => edit("name", "Host name")}>Rename</button>
        <button className="ghost small" onClick={() => edit("fusionhub", "FusionHub address")}>FusionHub…</button>
        <button className="ghost small" onClick={() => edit("notes", "Notes")}>Notes…</button>
        <button className="ghost small" title="A new token; the agent disconnects until it runs with the new token file" onClick={async () => {
          if (!(await confirmDialog(`Rotate ${h.name}'s token?`, { message: "The host disconnects at once and stays offline until its agent runs with the new token. Its instances keep recording.", confirmLabel: "Rotate" }))) return;
          try { onToken(await api.rotateHost(h.id)); onChanged(); } catch (e) { toast.error(e); }
        }}>Rotate token</button>
        <button className="ghost small" disabled={h.instances > 0} title={h.instances > 0 ? "Remove its instances first (each Site's Servers tab)" : undefined}
          onClick={async () => { if (await confirmDialog(`Remove host ${h.name}?`, { message: "Its token stops working. Nothing on the machine is deleted.", confirmLabel: "Remove", danger: true })) await act(() => api.removeHost(h.id), `${h.name} removed`)(); }}>Remove</button>
      </div>
    </div>
  );
}
