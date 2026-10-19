/**
 * /hub/hosts (hub administrators): the datacenter machines that run central recording instances, each with its
 * axiom-host agent dialing the hub, and every Site's central instance across customers. Built for a hundred hosts with
 * hundreds of instances: the hub filters, sorts and pages both lists (hub hostlist.py) and counts what needs attention
 * (GET /api/hub/fleet-summary) for the strip at the top, whose lines open the list they count.
 *   Hosts tab      one row per host: status, agent version, instances, CPU / RAM / GPU / disk bars, alerts; a row
 *                  opens the host's own page
 *   Instances tab  every instance in one table (InstancesTable.tsx): search, filters, sorts, a ⋯ menu per row, and
 *                  bulk changes for the safe ones (camera limit, raising storage)
 *   /hub/hosts/:id the host's card (capacity bars, what its GPUs are working through with ~30 min sparklines, Qwen time
 *                  per day of its instances, rename, FusionHub, notes, rotate token, remove), "Allocate instance…" and
 *                  its instances (each with its own Qwen time per day)
 * The tab and filters ride in the URL's query (fleetList.ts), so reload, Back and a pasted link keep them. Adding a host
 * shows its token once, with the command that starts the agent. Instances are allocated here only (customer, Site,
 * connection, storage, camera limit) and changed here; a Site's page shows its instance read-only. Once allocated, the
 * Site's admins add cameras on the instance's console and their addresses open on its firewall by themselves. An "Axiom
 * Bridge" connection needs no address here: the Site's admins enroll the bridge and map its cameras on the Site's
 * Servers tab (site/BridgesCard.tsx).
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { confirmDialog, promptDialog, toast } from "@site/ui";
import { type CentralInstance, type CentralMode, type FleetSummary, type Host, type HubSitesOrg, type QwenDays, ago, api, fmtTime, qs } from "../api";
import { dayShare, monthOf, qwenText, qwenTitle } from "../site/aiSchedule";
import {
  allocateFormError, capacityBars, defaultSubnet, fmtGB, nextSiteNumber, parseCameraLimit, seriesMax, sitesWithoutCentral, sparkPath, workRows,
} from "../central";
import { go, hostHref, navigate } from "../nav";
import {
  type AttentionItem, HOST_PROBLEM_LABEL, HOST_SORTS, HOST_SORT_LABEL, HOST_STATUSES, HOST_STATUS_LABEL, type HostsTab, type ListQuery, type MiniBar,
  activeAttention, attentionItems, barTone, defaultOrder, hostMiniBars, hostParams, parseQuery, querySearch, tabQuery, withQuery,
} from "./fleetList";
import { InstancesTable, Pager, SortPick } from "./InstancesTable";
import { QwenBars } from "./QwenBars";

/** The URL's query string, followed through Back / Forward and our own navigate() calls (nav.ts usePath reads only the path). */
function useSearch(): string {
  const [s, setS] = useState(location.search);
  useEffect(() => { const on = () => setS(location.search); addEventListener("popstate", on); return () => removeEventListener("popstate", on); }, []);
  return s;
}

export function HostsPage({ hostId }: { hostId?: string }) {
  const search = useSearch();
  const fixedTab: HostsTab | undefined = hostId ? "instances" : undefined;
  const query = useMemo(() => parseQuery(search, fixedTab), [search, fixedTab]);
  // a filter typed or picked replaces the URL; a tab, a page or an attention line is a step Back can undo
  const setQuery = useCallback((q: ListQuery, push = false) => navigate(location.pathname + querySearch(q, fixedTab), !push), [fixedTab]);
  const [summary, setSummary] = useState<FleetSummary | null>(null);
  const loadSummary = useCallback(() => { api.fleetSummary().then(setSummary).catch(() => {}); }, []);
  useEffect(() => { loadSummary(); const t = setInterval(loadSummary, 15000); return () => clearInterval(t); }, [loadSummary]);
  if (hostId) return <HostPage key={hostId} id={hostId} query={query} setQuery={setQuery} onChanged={loadSummary} />;
  return <Overview query={query} setQuery={setQuery} summary={summary} onChanged={loadSummary} />;
}

function Overview({ query, setQuery, summary, onChanged }: { query: ListQuery; setQuery: (q: ListQuery, push?: boolean) => void; summary: FleetSummary | null; onChanged: () => void }) {
  const [issued, setIssued] = useState<{ name: string; token: string; install: string } | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const tabs: { tab: HostsTab; label: string; n?: number }[] = [
    { tab: "hosts", label: "Hosts", n: summary?.hosts.total }, { tab: "instances", label: "Instances", n: summary?.instances.total }];
  return (
    <>
      <h2>Hosts <span className="muted small">datacenter machines running central recording, one server instance per customer Site</span></h2>
      {/* billing: every Site's Qwen use this month (hub ai_schedule.billing), also last month's */}
      <p className="small muted">Qwen use by Site for billing: <a href={`/api/hub/ai-usage?${qs({ format: "csv", month: monthOf(0) })}`}>this month (CSV)</a>
        {" · "}<a href={`/api/hub/ai-usage?${qs({ format: "csv", month: monthOf(1) })}`}>last month (CSV)</a></p>
      {issued && <TokenBox {...issued} onClose={() => setIssued(null)} />}
      <AddHost onAdded={(r, name) => { setIssued({ name, token: r.token, install: r.install }); setReloadKey((k) => k + 1); onChanged(); }} />
      <AttentionStrip summary={summary} query={query} onPick={(q) => setQuery(q, true)} />
      <div className="segmented site-tabs hosts-tabs" role="tablist">
        {tabs.map((t) => (
          <button key={t.tab} role="tab" aria-selected={query.tab === t.tab} className={query.tab === t.tab ? "active" : ""} onClick={() => setQuery(tabQuery(t.tab), true)}>
            {t.label}{t.n != null && <span className="muted small"> {t.n}</span>}
          </button>))}
      </div>
      {query.tab === "hosts"
        ? <HostsTable query={query} setQuery={setQuery} reloadKey={reloadKey} />
        : <InstancesTable query={query} setQuery={setQuery} hosts={summary?.host_index ?? []} onChanged={onChanged} />}
    </>
  );
}

/** What needs attention, one clickable line each (it opens the list it counts; clicked again, the filter goes), or one calm line. */
function AttentionStrip({ summary, query, onPick }: { summary: FleetSummary | null; query: ListQuery; onPick: (q: ListQuery) => void }) {
  if (!summary || !summary.hosts.total) return null;
  const items = attentionItems(summary);
  if (!items.length) return <p className="attention-ok small"><span className="dot ok" /> All hosts healthy</p>;
  const pick = (it: AttentionItem, on: boolean) => onPick(on ? tabQuery(it.to.tab) : it.to);
  return (
    <div className="attention-strip" role="group" aria-label="Needs attention">
      {items.map((it) => {
        const on = activeAttention(it, query);
        return <button key={it.key} type="button" className={`chip attention-chip ${it.tone} ${on ? "active" : ""}`} aria-pressed={on} title={it.title} onClick={() => pick(it, on)}>{it.text}</button>;
      })}
    </div>
  );
}

/** One row per host (the hub filters, sorts and pages); a row opens the host's page. */
function HostsTable({ query, setQuery, reloadKey }: { query: ListQuery; setQuery: (q: ListQuery, push?: boolean) => void; reloadKey: number }) {
  const [data, setData] = useState<{ hosts: Host[]; total: number; install: string } | null>(null);
  const params = JSON.stringify(hostParams(query));
  const load = useCallback(() => {
    api.hostsPage(JSON.parse(params)).then((r) => setData({ hosts: r.hosts, total: r.total, install: r.install })).catch((e) => toast.error(e));
  }, [params, reloadKey]);   // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { const t = setTimeout(load, 250); return () => clearTimeout(t); }, [load]);
  useEffect(() => { const t = setInterval(load, 15000); return () => clearInterval(t); }, [load]);
  const set = (change: Partial<ListQuery>) => setQuery(withQuery(query, change));
  const filtered = !!(query.q || query.status);
  const sort = query.sort || "name";
  const order = query.order || defaultOrder(sort);
  const open = (h: Host) => (e: React.MouseEvent) => {
    if ((e.target as HTMLElement).closest("a, button, input, select")) return;
    navigate(hostHref(h.id));
  };
  return (
    <div className="card">
      <div className="row list-toolbar">
        <input type="search" className="list-search" value={query.q} placeholder="Search hosts" aria-label="Search hosts" onChange={(e) => set({ q: e.target.value })} />
        <select value={query.status} aria-label="Status" onChange={(e) => set({ status: e.target.value })}>
          <option value="">Any status</option>
          {HOST_STATUSES.map((s) => <option key={s} value={s}>{HOST_STATUS_LABEL[s]}</option>)}
        </select>
        <SortPick sorts={HOST_SORTS} labels={HOST_SORT_LABEL} sort={sort} order={order}
          onChange={(s, o) => set({ sort: s === "name" ? "" : s, order: o === defaultOrder(s) ? "" : o })} />
      </div>
      {data === null ? <p className="muted small">Loading…</p> : data.total === 0 ? (
        filtered ? <p className="muted small">No host matches. <button className="linkish" onClick={() => setQuery({ ...query, q: "", status: "", page: 1 })}>Clear the filters</button></p>
          : <p className="muted">No hosts yet. Add one, then run its agent with the token: <code>{data.install}</code></p>
      ) : (
        <>
          <table className="hub-table stack hosts-table">
            <thead><tr><th>Host</th><th>Agent</th><th>Instances</th><th>CPU</th><th>RAM</th><th>GPU</th><th>Disk</th><th>Alerts</th></tr></thead>
            <tbody>{data.hosts.map((h) => {
              const bars = hostMiniBars(h.capacity);
              return (
                <tr key={h.id} className={`click-row ${h.online ? "" : "offline-row"}`} onClick={open(h)}>
                  <td className="lead">
                    <span className={`dot ${h.online ? "ok" : "bad"}`} title={h.online ? "Online" : `Offline · last seen ${ago(h.last_seen_at)}`} />{" "}
                    <a href={hostHref(h.id)} onClick={go(hostHref(h.id))}><strong>{h.name}</strong></a>
                    {h.hostname && <span className="muted small"> · {h.hostname}</span>}
                  </td>
                  <td className={`small ${h.problems?.includes("agent") ? "warn-text" : ""}`} data-label="Agent">{h.version ?? "—"}</td>
                  <td className="small" data-label="Instances">{h.instances}</td>
                  <td className="small" data-label="CPU"><Mini bar={bars.cpu} /></td>
                  <td className="small" data-label="RAM"><Mini bar={bars.ram} /></td>
                  <td className="small" data-label="GPU"><Mini bar={bars.gpu} /></td>
                  <td className="small" data-label="Disk"><Mini bar={bars.disk} />{h.room_gb != null && <span className="muted nowrap"> · room {fmtGB(Math.max(0, h.room_gb))}</span>}</td>
                  <td className="small"><HostAlerts h={h} /></td>
                </tr>);
            })}</tbody>
          </table>
          <Pager page={query.page} shown={data.hosts.length} total={data.total} onPage={(page) => setQuery({ ...query, page }, true)} />
        </>
      )}
    </div>
  );
}

function Mini({ bar }: { bar: MiniBar | null }) {
  if (!bar) return <span className="muted">—</span>;
  return (
    <span className="mini-bar" title={bar.text}>
      <span className={`cap-bar ${barTone(bar.pct)}`}><span style={{ width: `${bar.pct}%` }} /></span>
      <span className="muted">{bar.pct}%</span>
    </span>
  );
}

function HostAlerts({ h }: { h: Host }) {
  const probs = h.problems ?? [];
  if (!probs.length) return null;
  const queues = h.queue_alerts ?? [];
  return (
    <span className="problem-badges">{probs.map((p) => (
      <span key={p} className={`alert-kind host-problem-${p}`}
        title={p === "offline" ? (h.last_seen_at ? `Last seen ${ago(h.last_seen_at)}` : "Never connected") : p === "queue" ? queues.map((a) => a.text).filter(Boolean).join("\n") : undefined}>
        {p === "queue" && queues.length > 1 ? `${queues.length} queue alerts` : HOST_PROBLEM_LABEL[p]}</span>))}
    </span>
  );
}

/** One host's page: its card (everything about the machine and its actions) and its instances. */
function HostPage({ id, query, setQuery, onChanged }: { id: string; query: ListQuery; setQuery: (q: ListQuery, push?: boolean) => void; onChanged: () => void }) {
  const [host, setHost] = useState<Host | null | "gone">(null);
  const [issued, setIssued] = useState<{ token: string; install: string } | null>(null);
  const [allocating, setAllocating] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);
  const load = useCallback(() => {
    api.host(id).then(setHost).catch((e) => { if (String(e instanceof Error ? e.message : e).startsWith("404")) setHost("gone"); else toast.error(e); });
  }, [id]);
  useEffect(() => { load(); const t = setInterval(load, 15000); return () => clearInterval(t); }, [load]);
  const changed = () => { load(); onChanged(); };
  const name = host && host !== "gone" ? host.name : id;
  return (
    <>
      <h2 className="host-crumbs"><a href={hostHref()} onClick={go(hostHref())}>Hosts</a> <span className="crumb-sep">›</span> {name}</h2>
      {host === null ? <p className="muted">Loading…</p> : host === "gone" ? <p className="muted">No such host (removed?). <a href={hostHref()} onClick={go(hostHref())}>All hosts</a></p> : (
        <>
          {issued && <TokenBox name={host.name} {...issued} onClose={() => setIssued(null)} />}
          <HostCard h={host} onChanged={changed} onRemoved={() => { onChanged(); navigate(hostHref()); }} onToken={setIssued} onAllocate={() => setAllocating(true)} />
          <h3 className="host-instances-head">Instances on {host.name}</h3>
          <InstancesTable query={query} setQuery={setQuery} fixedHost={id} hosts={[]} onChanged={changed} reloadKey={reloadKey} />
          {allocating && <AllocateLoader host={host} onClose={() => setAllocating(false)} onDone={() => { setAllocating(false); setReloadKey((k) => k + 1); changed(); }} />}
        </>
      )}
    </>
  );
}

/** The allocate form needs every live instance (the Sites that already have one, the next site number): read once on opening. */
function AllocateLoader({ host, onClose, onDone }: { host: Host; onClose: () => void; onDone: () => void }) {
  const [central, setCentral] = useState<CentralInstance[] | null>(null);
  useEffect(() => { api.hubCentral().then(setCentral).catch((e) => { toast.error(e); onClose(); }); }, []);   // eslint-disable-line react-hooks/exhaustive-deps
  return central ? <AllocateDialog host={host} central={central} onClose={onClose} onDone={onDone} /> : null;
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
            <button type="button" className={mode === "bridge" ? "active" : ""} onClick={() => setMode("bridge")}>Axiom Bridge</button>
          </div>
          <span className="muted small">{mode === "vpn" ? "Encrypted, no camera port exposed. The BR1's LAN gets its own subnet: the Site's camera network."
            : mode === "bridge" ? "No way in needed (Starlink, cellular): a PC at the Site runs the Axiom Bridge, and the Site's admins map its cameras on the Site page."
            : "No VPN to run, but video and ONVIF travel unencrypted; forwards are locked to the datacenter's IP."}</span>
        </div>
        {mode === "vpn"
          ? <label className="field"><span>Camera network (the BR1's LAN)</span><input value={subnet} placeholder="10.20.7.0/24" onChange={(e) => setSubnet(e.target.value)} /></label>
          : mode === "forward" ? <label className="field"><span>The Site router's public IP or DNS name</span><input value={publicIp} placeholder="e.g. 93.184.216.34 or yard.dyndns.example.net" onChange={(e) => setPublicIp(e.target.value)} /></label>
          : null}
        <label className="field"><span>Storage (GB) {room != null && <span className="muted small">room on this host: {fmtGB(Math.max(0, room))}</span>}</span>
          <input inputMode="numeric" value={quota} onChange={(e) => setQuota(e.target.value.replace(/[^0-9]/g, ""))} /></label>
        <label className="field"><span>Camera limit <span className="muted small">blank = no limit</span></span>
          <input inputMode="numeric" value={limit} placeholder="no limit" onChange={(e) => setLimit(e.target.value.replace(/[^0-9]/g, ""))} /></label>
        <label className="field"><span>Server name in the Site</span><input value={name} maxLength={120} onChange={(e) => setName(e.target.value)} /></label>
        <p className="muted small">Once it runs, the Site's admins add cameras on its console; their addresses open on its firewall by themselves. More networks: "Site networks…" in its row's ⋯ menu.</p>
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

/** The host's Work section: Qwen (vLLM) on the A40, YOLO per GPU, sparklines; open queue alerts; Qwen time per day. */
function Work({ h }: { h: Host }) {
  const rows = workRows(h.capacity, h.work_trend);
  const qwen = h.qwen ? <QwenDaysRow q={h.qwen} /> : null;
  if (!rows) return <>{qwen && <div className="work">{qwen}</div>}<p className="muted small work-none">Work: no data{h.online ? " (its agent is older than 0.2.0, or just started)" : ""}</p></>;
  const hist = h.work_history ?? [];
  return (
    <div className="work">
      <div className="work-head small"><strong>Work</strong></div>
      {rows.map((r) => (
        <div key={r.key} className={`work-row small ${r.state}`} title={r.title}>
          <span className="cap-label muted">{r.label}</span><span>{r.text}</span>
        </div>))}
      {qwen}
      {hist.length > 1 && (
        <div className="sparks">
          <Spark label="Qwen waiting" values={hist.map((s) => s.vllm_waiting)} />
          <Spark label="YOLO queue" values={hist.map((s) => s.verify_q)} />
        </div>)}
    </div>
  );
}

/** Qwen time per UTC day of the host's instances together (what the shared vLLM did for them; per instance below). */
function QwenDaysRow({ q }: { q: QwenDays }) {
  const busiest = Math.max(0, ...q.days.map((d) => d.seconds));
  return (
    <div className="work-row small" title={qwenTitle(q)}>
      <span className="cap-label muted">Qwen per day</span>
      <span><QwenBars q={q} /> {qwenText(q)}{busiest > 0 ? <span className="muted"> · busiest day {dayShare(busiest)}% of 24 h</span> : null}</span>
    </div>
  );
}

/** The host's own page: the machine, its agent, capacity and work, and what can be done to it. */
function HostCard({ h, onChanged, onRemoved, onToken, onAllocate }: {
  h: Host; onChanged: () => void; onRemoved: () => void; onToken: (t: { token: string; install: string }) => void; onAllocate: () => void;
}) {
  const act = (f: () => Promise<unknown>, done?: string) => async () => { try { await f(); if (done) toast.success(done); onChanged(); } catch (e) { toast.error(e); } };
  const edit = async (field: "name" | "fusionhub" | "notes", title: string) => {
    const v = await promptDialog(title, { initial: (h[field] as string | null) ?? "", label: title });
    if (v == null || (field === "name" && !v.trim())) return;
    await act(() => api.updateHost(h.id, { [field]: v.trim() || null }))();
  };
  return (
    <div className={`site-card host-card ${h.online ? "" : "offline"}`}>
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
        <button className="ghost small" disabled={h.instances > 0} title={h.instances > 0 ? "Remove its instances first (the list below)" : undefined}
          onClick={async () => {
            if (!(await confirmDialog(`Remove host ${h.name}?`, { message: "Its token stops working. Nothing on the machine is deleted.", confirmLabel: "Remove", danger: true }))) return;
            try { await api.removeHost(h.id); toast.success(`${h.name} removed`); onRemoved(); } catch (e) { toast.error(e); }
          }}>Remove</button>
      </div>
    </div>
  );
}
