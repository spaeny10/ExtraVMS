/**
 * /hub/hosts (hub administrators): the datacenter machines that run central recording instances, each with its
 * axiom-host agent dialing the hub (online, CPU/RAM/GPU/disk, instances, and what its GPUs are working through: the
 * shared Qwen's requests and the instances' YOLO verify queues, with ~30 min sparklines), and every Site's central
 * instance across customers with its queues. Adding a host shows its token once, with the command that starts the agent.
 * Instances are allocated here only ("Allocate instance…" on a host: customer, Site, connection, storage, camera limit)
 * and changed here (storage, CPU/memory, camera limit, Site networks, removal); a Site's page shows its instance
 * read-only. Once allocated, the Site's admins add cameras on the instance's console and their addresses open on its
 * firewall by themselves.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { confirmDialog, promptDialog, toast } from "@site/ui";
import { type CentralInstance, type CentralMode, type Host, type HubSitesOrg, ago, api, fmtTime } from "../api";
import {
  PHASE_LABEL, allocateFormError, camerasText, capacityBars, defaultSubnet, fmtGB, instanceWork, nextSiteNumber, overLimit, parseCameraLimit,
  queueText, queueTitle, quotaText, resourcesFormError, resourcesText, seriesMax, settling, shortGpu, sitesWithoutCentral, sparkPath, workRows,
} from "../central";
import { go, siteHref } from "../nav";
import { SiteNetworks } from "./SiteNetworks";

export function HostsPage() {
  const [hosts, setHosts] = useState<Host[] | null>(null);
  const [install, setInstall] = useState("");
  const [central, setCentral] = useState<CentralInstance[]>([]);
  const [issued, setIssued] = useState<{ name: string; token: string; install: string } | null>(null);
  const [allocating, setAllocating] = useState<Host | null>(null);
  const [networks, setNetworks] = useState<CentralInstance | null>(null);
  const [removing, setRemoving] = useState<CentralInstance | null>(null);
  const [resizing, setResizing] = useState<CentralInstance | null>(null);
  const load = useCallback(() => {
    api.hosts().then((r) => { setHosts(r.hosts); setInstall(r.install); }).catch((e) => toast.error(e));
    api.hubCentral().then(setCentral).catch(() => setCentral([]));
  }, []);
  // while an instance is coming up (or going away) the page follows it closely
  const busy = central.some(settling);
  useEffect(() => { load(); const t = setInterval(load, busy ? 3000 : 15000); return () => clearInterval(t); }, [load, busy]);
  return (
    <>
      <h2>Hosts <span className="muted small">datacenter machines running central recording, one server instance per customer Site</span></h2>
      {issued && <TokenBox {...issued} onClose={() => setIssued(null)} />}
      <AddHost onAdded={(r, name) => { setIssued({ name, token: r.token, install: r.install }); load(); }} />
      {hosts === null ? <p className="muted">Loading…</p> : hosts.length === 0 ? (
        <p className="muted">No hosts yet. Add one, then run its agent with the token: <code>{install}</code></p>
      ) : (
        <div className="host-grid">{hosts.map((h) => <HostCard key={h.id} h={h} onChanged={load} onToken={(t) => setIssued({ name: h.name, ...t })} onAllocate={() => setAllocating(h)} />)}</div>
      )}
      <div className="card">
        <h3 style={{ marginTop: 0 }}>Central instances <span className="muted small">{central.length} across every customer</span></h3>
        {central.length === 0 ? <p className="muted small">None yet: use "Allocate instance…" on a host.</p> : (
          <table className="hub-table stack">
            <thead><tr><th>Site</th><th>Customer</th><th>Host</th><th>Network</th><th>GPU</th><th>Storage</th><th>Cameras</th><th>Queues</th><th>State</th><th /></tr></thead>
            <tbody>{central.map((c) => { const q = instanceWork(hosts, c); return (
              <tr key={c.id}>
                <td className="lead"><a href={siteHref(c.location_id, "servers")} onClick={go(siteHref(c.location_id, "servers"))}>{c.location_name ?? c.location_id}</a>
                  {c.site_number != null && <span className="muted small"> · site #{c.site_number}</span>}</td>
                <td>{c.org_name ?? c.org_id}</td>
                <td>{c.host_name ?? c.host_id}{c.host_online === false ? <span className="muted small"> (offline)</span> : null}</td>
                <td className="small" data-label="Network" title={c.camera_network?.sync_error ? `Camera addresses not on the host: ${c.camera_network.sync_error}` : undefined}>
                  {c.mode === "vpn" ? `VPN · ${c.subnet ?? "—"}` : `Port forward · ${c.public_ip ?? "—"}`}
                  {c.camera_network?.pending ? <span className="bad"> · camera addresses not applied yet</span> : null}</td>
                <td className="small" data-label="GPU">{c.gpu != null ? `${c.gpu}${c.gpu_name ? ` · ${shortGpu(c.gpu_name)}` : ""}` : "CPU"}
                  {(c.cpus != null || c.mem_gb != null) && <span className="muted"> · {resourcesText(c.cpus, c.mem_gb)}</span>}</td>
                <td className="small" data-label="Storage">{quotaText(c.used_gb, c.quota_gb)}</td>
                <td className={`small ${overLimit(c.camera_count, c.camera_limit) ? "bad" : ""}`} data-label="Cameras">{camerasText(c.camera_count, c.camera_limit)}</td>
                <td className={`small queue-cell ${q.growing ? "warn" : !q.work ? "muted" : q.work.ok ? "" : "bad"}`} data-label="Queues" title={queueTitle(q.work, q.growing)}>
                  {queueText(q.work)}{q.growing ? " ↑" : ""}</td>
                <td className="small" title={c.last_error ?? undefined}>{PHASE_LABEL[c.phase] ?? c.phase}{c.state === "failed" && c.last_error ? `: ${c.last_error.slice(0, 80)}` : ""}</td>
                <td><InstanceActions ci={c} onChanged={load} onNetworks={() => setNetworks(c)} onResources={() => setResizing(c)} onRemove={() => setRemoving(c)} /></td>
              </tr>); })}
            </tbody>
          </table>
        )}
      </div>
      {allocating && <AllocateDialog host={allocating} central={central} onClose={() => setAllocating(null)} onDone={() => { setAllocating(null); load(); }} />}
      {networks && <SiteNetworks ci={networks} onClose={() => setNetworks(null)} onSaved={() => { setNetworks(null); load(); }} />}
      {removing && <RemoveDialog ci={removing} onClose={() => setRemoving(null)} onDone={() => { setRemoving(null); load(); }} />}
      {resizing && <ResourcesDialog ci={resizing} hostCpus={hosts?.find((h) => h.id === resizing.host_id)?.capacity?.cpus ?? null}
        onClose={() => setResizing(null)} onDone={() => { setResizing(null); load(); }} />}
    </>
  );
}

/** Storage, CPU/memory, camera limit, Site networks and removal of one instance (the table's last column). */
function InstanceActions({ ci, onChanged, onNetworks, onResources, onRemove }: { ci: CentralInstance; onChanged: () => void; onNetworks: () => void; onResources: () => void; onRemove: () => void }) {
  const site = ci.location_name ?? ci.location_id;
  const live = ci.state !== "deleting" && ci.state !== "deleted";
  const offline = ci.host_online === false;
  const storage = async () => {
    const v = await promptDialog(`Storage for ${site} (GB)`, { initial: String(ci.quota_gb), label: "GB",
      message: "The instance's disk quota on its host, at least 10 GB. Below what it uses, it deletes its oldest recordings." });
    if (v == null) return;
    const q = Number(v.trim());
    if (!/^\d+$/.test(v.trim()) || q < 10) { toast.error("The storage quota is a whole number of GB, at least 10"); return; }
    try { await api.updateCentral(ci.location_id, ci.id, { quota_gb: q }); toast.success(`Storage set to ${fmtGB(q)}`); onChanged(); } catch (e) { toast.error(e); }
  };
  const limit = async () => {
    const count = ci.camera_count ?? 0;
    const v = await promptDialog(`Camera limit for ${site}`, { initial: ci.camera_limit == null ? "" : String(ci.camera_limit), label: "Cameras (blank = no limit)",
      message: `It has ${count} camera${count === 1 ? "" : "s"}. Cameras beyond the limit are refused when they are added.` });
    if (v == null) return;
    const lim = parseCameraLimit(v);
    if (lim.error) { toast.error(lim.error); return; }
    if (lim.value != null && lim.value < count && !(await confirmDialog(`Lower the limit below its ${count} cameras?`, {
      message: "The cameras it has keep recording; new ones are refused until it has fewer than the limit.", confirmLabel: "Lower it" }))) return;
    try {
      await api.updateCentral(ci.location_id, ci.id, { camera_limit: lim.value });
      toast.success(lim.value == null ? "No camera limit" : `Camera limit set to ${lim.value}`);
      onChanged();
    } catch (e) { toast.error(e); }
  };
  return (
    <div className="central-row-actions">
      <button className="ghost small" disabled={!live || offline} title={offline ? "The host is offline" : undefined} onClick={storage}>Change storage…</button>
      <button className="ghost small" disabled={!live || offline} title={offline ? "The host is offline" : "CPU and memory limits (the instance restarts)"} onClick={onResources}>Change CPU/memory…</button>
      <button className="ghost small" disabled={!live} onClick={limit}>Change camera limit…</button>
      <button className="ghost small" disabled={!live || offline} title={offline ? "The host is offline" : undefined} onClick={onNetworks}>Site networks…</button>
      <button className="ghost small" disabled={ci.state === "deleted"} onClick={onRemove}>Remove…</button>
    </div>
  );
}

/** CPU and memory limits of an instance: applied live by its host (docker update), recreated only if that fails. */
function ResourcesDialog({ ci, hostCpus, onClose, onDone }: { ci: CentralInstance; hostCpus: number | null; onClose: () => void; onDone: () => void }) {
  const [cpus, setCpus] = useState(ci.cpus != null ? String(ci.cpus) : "");
  const [mem, setMem] = useState(ci.mem_gb != null ? String(ci.mem_gb) : "");
  const [busy, setBusy] = useState(false);
  const site = ci.location_name ?? ci.location_id;
  const err = resourcesFormError(cpus, mem, hostCpus);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const body: { cpus?: number; mem_gb?: number } = {};
    if (cpus.trim() && Number(cpus) !== ci.cpus) body.cpus = Number(cpus);
    if (mem.trim() && Number(mem) !== ci.mem_gb) body.mem_gb = Number(mem);
    if (!Object.keys(body).length) { toast.success("Nothing changed"); onClose(); return; }
    setBusy(true);
    try {
      const r = await api.setCentralResources(ci.location_id, ci.id, body);
      toast.success(`${site}: ${resourcesText(r.cpus ?? body.cpus, r.mem_gb ?? body.mem_gb)}`);
      onDone();
    } catch (er) { toast.error(er); } finally { setBusy(false); }
  };
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <form className="modal central-dialog central-form" onClick={(e) => e.stopPropagation()} onSubmit={submit} style={{ maxWidth: 460 }}>
        <header className="modal-head"><h2>CPU and memory for {site}</h2><button type="button" className="ghost" onClick={onClose} aria-label="Close">✕</button></header>
        <p className="small muted">Now: {resourcesText(ci.cpus, ci.mem_gb)}{hostCpus ? ` · the host has ${hostCpus} CPUs` : ""}. Blank keeps the current value.</p>
        <label className="field"><span>CPUs <span className="muted small">1-64</span></span>
          <input inputMode="decimal" value={cpus} placeholder="e.g. 8" onChange={(e) => setCpus(e.target.value.replace(/[^0-9.]/g, ""))} /></label>
        <label className="field"><span>Memory (GB) <span className="muted small">2-512</span></span>
          <input inputMode="decimal" value={mem} placeholder="e.g. 16" onChange={(e) => setMem(e.target.value.replace(/[^0-9.]/g, ""))} /></label>
        <p className="small">Applied to the running instance, with no break in recording. Only if the host can't apply them live (for example less memory than the instance uses now) does it restart the instance: about 30 seconds without recording.</p>
        <div className="row">
          <button type="submit" disabled={busy || !!err}>{busy ? "Restarting…" : "Apply and restart"}</button>
          <button type="button" className="ghost" onClick={onClose}>Cancel</button>
          {err && <span className="muted small">{err}</span>}
        </div>
      </form>
    </div>
  );
}

/** Remove an instance: deleted on its host (recordings kept unless ticked), its server retired in the Site. */
function RemoveDialog({ ci, onClose, onDone }: { ci: CentralInstance; onClose: () => void; onDone: () => void }) {
  const [purge, setPurge] = useState(false);
  const [busy, setBusy] = useState(false);
  const site = ci.location_name ?? ci.location_id;
  const remove = async () => {
    setBusy(true);
    try { await api.removeCentral(ci.location_id, ci.id, { purge }); toast.success(`Central recording removed from ${site}`); onDone(); }
    catch (e) {
      toast.error(e);
      if (await confirmDialog("Remove it anyway?", { message: "The hub forgets the instance even though its host could not delete it (it may keep running there until removed by hand).", confirmLabel: "Remove anyway", danger: true })) {
        try { await api.removeCentral(ci.location_id, ci.id, { purge, force: true }); toast.success("Removed at the hub"); onDone(); } catch (e2) { toast.error(e2); }
      }
    } finally { setBusy(false); }
  };
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal central-dialog" onClick={(e) => e.stopPropagation()} style={{ maxWidth: 520 }}>
        <header className="modal-head"><h2>Remove central recording for {site}?</h2><button type="button" className="ghost" onClick={onClose} aria-label="Close">✕</button></header>
        <p className="small">The instance is deleted on {ci.host_name ?? "its host"} and its server is retired in the Site. {purge ? <strong>All its recordings are deleted too. This cannot be undone.</strong> : "Its recordings and database stay on the host."}</p>
        <label className="small row"><input type="checkbox" checked={purge} onChange={(e) => setPurge(e.target.checked)} /> delete recordings too</label>
        <div className="row">
          <button className="danger" disabled={busy} onClick={remove}>Remove</button>
          <button className="ghost" onClick={onClose}>Cancel</button>
        </div>
      </div>
    </div>
  );
}

/** Allocate an instance on this host for one customer's Site: connection, storage (the host's room shown), camera limit. */
function AllocateDialog({ host, central, onClose, onDone }: { host: Host; central: CentralInstance[]; onClose: () => void; onDone: () => void }) {
  const [orgs, setOrgs] = useState<HubSitesOrg[] | null>(null);
  const [org, setOrg] = useState("");
  const [site, setSite] = useState("");
  const [mode, setMode] = useState<CentralMode>("vpn");
  // prefill with the site number the hub will most likely assign (it decides; an untouched field is left to it)
  const next = useMemo(() => nextSiteNumber(central.filter((c) => c.state !== "deleted").map((c) => c.site_number)), [central]);
  const [subnet, setSubnet] = useState(() => (next ? defaultSubnet(next) : ""));
  const [publicIp, setPublicIp] = useState("");
  const [quota, setQuota] = useState("2000");
  const [limit, setLimit] = useState("");
  const [name, setName] = useState("Central");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    api.hubSites().then((o) => setOrgs([...o].sort((a, b) => a.org.name.localeCompare(b.org.name)))).catch((e) => { toast.error(e); setOrgs([]); });
  }, []);
  const free = useMemo(() => (orgs && org ? sitesWithoutCentral(orgs, org, central) : []), [orgs, org, central]);
  const room = host.room_gb ?? null;
  const err = allocateFormError({ org, site, mode, subnet, public_ip: publicIp, quota_gb: quota, camera_limit: limit }, room);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      const edited = subnet.trim() && !(next && subnet.trim() === defaultSubnet(next));
      await api.provisionCentral(site, { host_id: host.id, mode, quota_gb: Number(quota), camera_limit: parseCameraLimit(limit).value, name: name.trim() || null,
        subnet: mode === "vpn" && edited ? subnet.trim() : null, public_ip: mode === "forward" ? publicIp.trim() : null });
      toast.success("Provisioning started");
      onDone();
    } catch (er) { toast.error(er); } finally { setBusy(false); }
  };
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <form className="modal central-dialog central-form" onClick={(e) => e.stopPropagation()} onSubmit={submit} style={{ maxWidth: 560 }}>
        <header className="modal-head"><h2>Allocate an instance on {host.name}</h2><button type="button" className="ghost" onClick={onClose} aria-label="Close">✕</button></header>
        <label className="field"><span>Customer</span>
          <select value={org} onChange={(e) => { setOrg(e.target.value); setSite(""); }} disabled={!orgs}>
            <option value="">{orgs ? "Choose a customer…" : "Loading…"}</option>
            {(orgs ?? []).map((o) => <option key={o.org.id} value={o.org.id}>{o.org.name}</option>)}
          </select>
        </label>
        <label className="field"><span>Site</span>
          <select value={site} onChange={(e) => setSite(e.target.value)} disabled={!org}>
            <option value="">{!org ? "Choose the customer first" : free.length ? "Choose a Site…" : "Every Site of this customer has central recording"}</option>
            {free.map((l) => <option key={l.id} value={l.id}>{l.name}</option>)}
          </select>
        </label>
        <div className="field"><span>Connection to the Site</span>
          <div className="segmented small-seg">
            <button type="button" className={mode === "vpn" ? "active" : ""} onClick={() => setMode("vpn")}>SpeedFusion VPN</button>
            <button type="button" className={mode === "forward" ? "active" : ""} onClick={() => setMode("forward")}>Port forwarding</button>
          </div>
          <span className="muted small">{mode === "vpn" ? "Encrypted, no camera port exposed. The BR1's LAN gets its own subnet: the Site's camera network." : "No VPN to run, but video and ONVIF travel unencrypted; forwards are locked to the datacenter's IP."}</span>
        </div>
        {mode === "vpn"
          ? <label className="field"><span>Camera network (the BR1's LAN)</span><input value={subnet} placeholder="10.20.7.0/24" onChange={(e) => setSubnet(e.target.value)} /></label>
          : <label className="field"><span>The Site router's public IP or DNS name</span><input value={publicIp} placeholder="e.g. 93.184.216.34 or yard.dyndns.example.net" onChange={(e) => setPublicIp(e.target.value)} /></label>}
        <label className="field"><span>Storage (GB) {room != null && <span className="muted small">room on this host: {fmtGB(Math.max(0, room))}</span>}</span>
          <input inputMode="numeric" value={quota} onChange={(e) => setQuota(e.target.value.replace(/[^0-9]/g, ""))} /></label>
        <label className="field"><span>Camera limit <span className="muted small">blank = no limit</span></span>
          <input inputMode="numeric" value={limit} placeholder="no limit" onChange={(e) => setLimit(e.target.value.replace(/[^0-9]/g, ""))} /></label>
        <label className="field"><span>Server name in the Site</span><input value={name} maxLength={120} onChange={(e) => setName(e.target.value)} /></label>
        <p className="muted small">Once it runs, the Site's admins add cameras on its console; their addresses open on its firewall by themselves. More networks: "Site networks…" in the list below.</p>
        <div className="row">
          <button type="submit" disabled={busy || !!err}>Allocate</button>
          <button type="button" className="ghost" onClick={onClose}>Cancel</button>
          {err && <span className="muted small">{err}</span>}
        </div>
      </form>
    </div>
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

/** A tiny line chart (inline SVG, theme colors) of one series over the hub's ~30 minute history. */
function Spark({ values, label }: { values: (number | null)[]; label: string }) {
  const W = 120, H = 24;
  const d = sparkPath(values, W, H);
  const top = seriesMax(values);
  return (
    <span className="spark small" title={`${label}: the last ${values.length} heartbeats (about ${Math.round(values.length / 2)} min), up to ${top ?? 0}`}>
      <span className="muted">{label}</span>
      <svg viewBox={`-1 -1 ${W + 2} ${H + 2}`} width={W} height={H} role="img" aria-label={`${label} sparkline`} preserveAspectRatio="none">
        <line x1={0} y1={H} x2={W} y2={H} className="spark-base" />
        {d && <path d={d} />}
      </svg>
      <span className="muted">{top ?? "—"}</span>
    </span>
  );
}

/** The host's Work section: Qwen (vLLM) on the A40, YOLO per GPU, sparklines; open queue alerts. */
function Work({ h }: { h: Host }) {
  const rows = workRows(h.capacity, h.work_trend);
  if (!rows) return <p className="muted small work-none">Work: no data{h.online ? " (its agent is older than 0.2.0, or just started)" : ""}</p>;
  const hist = h.work_history ?? [];
  return (
    <div className="work">
      <div className="work-head small"><strong>Work</strong></div>
      {rows.map((r) => (
        <div key={r.key} className={`work-row small ${r.state}`} title={r.title}>
          <span className="cap-label muted">{r.label}</span><span>{r.text}</span>
        </div>))}
      {hist.length > 1 && (
        <div className="sparks">
          <Spark label="Qwen waiting" values={hist.map((s) => s.vllm_waiting)} />
          <Spark label="YOLO queue" values={hist.map((s) => s.verify_q)} />
        </div>)}
    </div>
  );
}

function HostCard({ h, onChanged, onToken, onAllocate }: { h: Host; onChanged: () => void; onToken: (t: { token: string; install: string }) => void; onAllocate: () => void }) {
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
      {(h.queue_alerts ?? []).map((a) => <div key={a.key} className="alerts">⚠ {a.text ?? "A work queue is growing"} · since {fmtTime(a.opened_at)}</div>)}
      <div className="stats small">
        <div><span>Machine</span> {h.hostname ?? "—"}{h.agent_ip ? ` · ${h.agent_ip}` : ""}</div>
        <div><span>Agent</span> {h.version ?? "—"}</div>
        <div><span>Instances</span> {h.instances}{h.quota_gb ? ` · ${fmtGB(h.quota_gb)} of quota` : ""}{h.room_gb != null ? ` · room for ${fmtGB(Math.max(0, h.room_gb))}` : ""}</div>
        <div><span>FusionHub</span> {h.fusionhub ?? "—"}</div>
      </div>
      <Bars h={h} />
      <Work h={h} />
      {h.notes && <p className="muted small" style={{ whiteSpace: "pre-wrap" }}>{h.notes}</p>}
      <div className="row server-actions">
        <button className="small" disabled={!h.online} title={h.online ? "Place a central instance for a customer's Site on this host" : "The host is offline"} onClick={onAllocate}>Allocate instance…</button>
        <button className="ghost small" onClick={() => edit("name", "Host name")}>Rename</button>
        <button className="ghost small" onClick={() => edit("fusionhub", "FusionHub address")}>FusionHub…</button>
        <button className="ghost small" onClick={() => edit("notes", "Notes")}>Notes…</button>
        <button className="ghost small" title="A new token; the agent disconnects until it runs with the new token file" onClick={async () => {
          if (!(await confirmDialog(`Rotate ${h.name}'s token?`, { message: "The host disconnects at once and stays offline until its agent runs with the new token. Its instances keep recording.", confirmLabel: "Rotate" }))) return;
          try { onToken(await api.rotateHost(h.id)); onChanged(); } catch (e) { toast.error(e); }
        }}>Rotate token</button>
        <button className="ghost small" disabled={h.instances > 0} title={h.instances > 0 ? "Remove its instances first (Central instances below)" : undefined}
          onClick={async () => { if (await confirmDialog(`Remove host ${h.name}?`, { message: "Its token stops working. Nothing on the machine is deleted.", confirmLabel: "Remove", danger: true })) await act(() => api.removeHost(h.id), `${h.name} removed`)(); }}>Remove</button>
      </div>
    </div>
  );
}
