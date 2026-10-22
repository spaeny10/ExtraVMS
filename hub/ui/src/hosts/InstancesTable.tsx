/**
 * The Hosts page's instance list (the Instances tab across every host, and one host's page with `fixedHost`): one
 * page of the hub's filtered, sorted list (GET /api/hub/central with paging, hub hostlist.py), its filters in the URL
 * (fleetList.ts), a ⋯ menu per row with what used to be its five buttons (storage, CPU/memory, camera limit, Site
 * networks, removal), its server's Qwen seconds per day (the A40's shared vLLM), and checkboxes for the changes that are safe on many at once: the camera limit, and raising the
 * storage quota (never lowering it, and never Remove). A bulk change lists the instances in a dialog before it runs.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { confirmDialog, menuKeys, promptDialog, toast, useDismiss } from "@site/ui";
import { type CentralInstance, type FleetSummary, api } from "../api";
import {
  PHASE_LABEL, camerasText, dataText, dataTitle, fmtGB, overLimit, parseCameraLimit, queueText, queueTitle, quotaText, resourcesFormError, resourcesText,
  settling, shortGpu,
} from "../central";
import { go, hostHref, siteHref } from "../nav";
import {
  type BulkAction, INSTANCE_MODES, INSTANCE_PROBLEMS, INSTANCE_SORTS, INSTANCE_SORT_LABEL, INSTANCE_STATES, type ListQuery, MODE_LABEL, PROBLEM_LABEL,
  STATE_LABEL, bulkPlan, bulkValueError, camerasDown, defaultOrder, downText, imageTitle, instanceParams, lastPage, pageText, rowProblems, withQuery,
} from "./fleetList";
import { qwenText, qwenTitle } from "../site/aiSchedule";
import { QwenBars } from "./QwenBars";
import { SiteNetworks } from "./SiteNetworks";

type Props = {
  /** push: a step Back undoes (a page); otherwise the URL is replaced (a filter typed or picked) */
  query: ListQuery; setQuery: (q: ListQuery, push?: boolean) => void;
  /** one host's page: only its instances, no Host column or filter */
  fixedHost?: string;
  hosts: FleetSummary["host_index"];
  /** after any change: the attention strip (and a host's card) follow */
  onChanged: () => void;
  /** bumped by the page when something else changed the list (an instance allocated) */
  reloadKey?: number;
};

export function InstancesTable({ query, setQuery, fixedHost, hosts, onChanged, reloadKey = 0 }: Props) {
  const [data, setData] = useState<{ items: CentralInstance[]; total: number } | null>(null);
  const [selected, setSelected] = useState<Map<string, CentralInstance>>(new Map());
  const [bulk, setBulk] = useState<BulkAction | null>(null);
  const [networks, setNetworks] = useState<CentralInstance | null>(null);
  const [removing, setRemoving] = useState<CentralInstance | null>(null);
  const [resizing, setResizing] = useState<CentralInstance | null>(null);
  const params = JSON.stringify(instanceParams(query, fixedHost));
  const load = useCallback(() => {
    api.hubCentralPage(JSON.parse(params)).then((r) => setData({ items: r.items, total: r.total })).catch((e) => { toast.error(e); setData({ items: [], total: 0 }); });
  }, [params, reloadKey]);   // eslint-disable-line react-hooks/exhaustive-deps
  // typing in the search box changes the query on every key: ask the hub once it pauses
  useEffect(() => { const t = setTimeout(load, 250); return () => clearTimeout(t); }, [load]);
  // a new filter or page starts a new selection (what is ticked stays what is shown)
  useEffect(() => { setSelected(new Map()); }, [params]);
  // while an instance is coming up (or going away) the list follows it closely
  const busy = !!data?.items.some(settling);
  useEffect(() => { const t = setInterval(load, busy ? 3000 : 15000); return () => clearInterval(t); }, [load, busy]);
  const changed = () => { load(); onChanged(); };
  const items = data?.items ?? [];
  const set = (change: Partial<ListQuery>) => setQuery(withQuery(query, change));
  const filtered = !!(query.q || query.state || (!fixedHost && query.host) || query.mode || query.problem);
  const allTicked = items.length > 0 && items.every((c) => selected.has(c.id));
  const tick = (c: CentralInstance, on: boolean) => setSelected((m) => { const n = new Map(m); if (on) n.set(c.id, c); else n.delete(c.id); return n; });
  const sort = query.sort || "created";
  const order = query.order || defaultOrder(sort);
  return (
    <div className="card instances-card">
      <div className="row list-toolbar">
        <input type="search" className="list-search" value={query.q} placeholder={fixedHost ? "Search Site or customer" : "Search Site, customer or host"}
          aria-label="Search instances" onChange={(e) => set({ q: e.target.value })} />
        <select value={query.state} aria-label="State" onChange={(e) => set({ state: e.target.value })}>
          <option value="">Any state</option>
          {INSTANCE_STATES.map((s) => <option key={s} value={s}>{STATE_LABEL[s]}</option>)}
        </select>
        {!fixedHost && (
          <select value={query.host} aria-label="Host" onChange={(e) => set({ host: e.target.value })}>
            <option value="">Every host</option>
            {hosts.map((h) => <option key={h.id} value={h.id}>{h.name}{h.online ? "" : " (offline)"}</option>)}
          </select>)}
        <select value={query.mode} aria-label="Connection" onChange={(e) => set({ mode: e.target.value })}>
          <option value="">Any connection</option>
          {INSTANCE_MODES.map((m) => <option key={m} value={m}>{MODE_LABEL[m]}</option>)}
        </select>
        <select value={query.problem} aria-label="Problem" onChange={(e) => set({ problem: e.target.value })}>
          <option value="">Any or no problem</option>
          {INSTANCE_PROBLEMS.map((p) => <option key={p} value={p}>{PROBLEM_LABEL[p]}</option>)}
        </select>
        <SortPick sorts={INSTANCE_SORTS.filter((s) => !(fixedHost && s === "host"))} labels={INSTANCE_SORT_LABEL} sort={sort} order={order}
          onChange={(s, o) => set({ sort: s === "created" ? "" : s, order: o === defaultOrder(s) ? "" : o })} />
      </div>
      {selected.size > 0 && (
        <div className="row bulk-bar" role="region" aria-label="Selected instances">
          <strong className="small">{selected.size} selected</strong>
          <button className="ghost small" onClick={() => setBulk("limit")}>Change camera limit…</button>
          <button className="ghost small" onClick={() => setBulk("storage")}>Raise storage…</button>
          <button className="linkish" onClick={() => setSelected(new Map())}>Clear</button>
        </div>)}
      {data === null ? <p className="muted small">Loading…</p> : data.total === 0 ? (
        <p className="muted small">{filtered ? <>No instance matches. <button className="linkish" onClick={() => setQuery({ ...query, q: "", state: "", host: "", mode: "", problem: "", page: 1 })}>Clear the filters</button></>
          : fixedHost ? "No instances on this host yet: use \"Allocate instance…\" above." : "None yet: open a host and use \"Allocate instance…\"."}</p>
      ) : (
        <>
          <table className="hub-table stack instances-table">
            <thead><tr>
              <th><input type="checkbox" className="row-tick" aria-label="Select every instance on this page" checked={allTicked}
                onChange={(e) => setSelected(e.target.checked ? new Map(items.map((c) => [c.id, c])) : new Map())} /> Site</th><th>Customer</th>{!fixedHost && <th>Host</th>}<th>Network</th><th>GPU</th><th>Storage</th><th>Data</th><th>Cameras</th><th>Queues</th><th>Qwen</th><th>State</th><th />
            </tr></thead>
            <tbody>{items.map((c) => (
              <Row key={c.id} c={c} showHost={!fixedHost} ticked={selected.has(c.id)} onTick={(on) => tick(c, on)} onChanged={changed}
                onNetworks={() => setNetworks(c)} onResources={() => setResizing(c)} onRemove={() => setRemoving(c)} />))}
            </tbody>
          </table>
          <Pager page={query.page} shown={items.length} total={data.total} onPage={(page) => setQuery({ ...query, page }, true)} />
        </>
      )}
      {bulk && <BulkDialog action={bulk} selected={[...selected.values()]} onClose={() => setBulk(null)}
        onDone={() => { setBulk(null); setSelected(new Map()); changed(); }} />}
      {networks && <SiteNetworks ci={networks} onClose={() => setNetworks(null)} onSaved={() => { setNetworks(null); changed(); }} />}
      {removing && <RemoveDialog ci={removing} onClose={() => setRemoving(null)} onDone={() => { setRemoving(null); changed(); }} />}
      {resizing && <ResourcesDialog ci={resizing} hostCpus={resizing.host_cpus ?? null} onClose={() => setResizing(null)} onDone={() => { setResizing(null); changed(); }} />}
    </div>
  );
}

function Row({ c, showHost, ticked, onTick, onChanged, onNetworks, onResources, onRemove }: {
  c: CentralInstance; showHost: boolean; ticked: boolean; onTick: (on: boolean) => void; onChanged: () => void;
  onNetworks: () => void; onResources: () => void; onRemove: () => void;
}) {
  const site = c.location_name ?? c.location_id;
  const w = c.work ?? null;
  const down = camerasDown(c);
  const probs = rowProblems(c).filter((p) => p !== "down" || downText(c));
  return (
    <tr className={ticked ? "ticked" : ""}>
      <td className="lead">
        <input type="checkbox" className="row-tick" aria-label={`Select ${site}`} checked={ticked} onChange={(e) => onTick(e.target.checked)} />{" "}
        <a href={siteHref(c.location_id, "servers")} onClick={go(siteHref(c.location_id, "servers"))}>{site}</a>
        {c.site_number != null && <span className="muted small"> · site #{c.site_number}</span>}
      </td>
      <td className="small">{c.org_name ?? c.org_id}</td>
      {showHost && <td className="small" data-label="Host">
        {c.host_id ? <a href={hostHref(c.host_id)} onClick={go(hostHref(c.host_id))}>{c.host_name ?? c.host_id}</a> : "—"}
        {c.host_online === false ? <span className="muted"> (offline)</span> : null}</td>}
      <td className="small" data-label="Network" title={c.camera_network?.sync_error ? `Camera addresses not on the host: ${c.camera_network.sync_error}` : undefined}>
        {c.mode === "vpn" ? `VPN · ${c.subnet ?? "—"}` : c.mode === "bridge" ? "Axiom Bridge" : `Port forward · ${c.public_ip ?? "—"}`}
        {c.camera_network?.pending ? <span className="bad"> · camera addresses not applied yet</span> : null}</td>
      <td className="small" data-label="GPU">{c.gpu != null ? `${c.gpu}${c.gpu_name ? ` · ${shortGpu(c.gpu_name)}` : ""}` : "CPU"}
        {(c.cpus != null || c.mem_gb != null) && <span className="muted"> · {resourcesText(c.cpus, c.mem_gb)}</span>}</td>
      <td className={`small ${c.problems?.includes("storage") ? "warn-text" : ""}`} data-label="Storage">{quotaText(c.used_gb, c.quota_gb)}</td>
      {/* its data area (database, event media; agent 0.3.2+): "12 GB of 150 GB" */}
      <td className={`small ${c.problems?.includes("data") ? "bad" : ""}`} data-label="Data"
        title={dataTitle(c.instance_used_gb, c.instance_quota_gb, c.instance_free_gb)}>{dataText(c.instance_used_gb, c.instance_quota_gb)}</td>
      <td className={`small ${overLimit(c.camera_count, c.camera_limit) ? "bad" : ""}`} data-label="Cameras">{camerasText(c.camera_count, c.camera_limit)}
        {down > 0 && <span className="warn-text"> · {down} down</span>}</td>
      <td className={`small queue-cell ${c.queue_growing ? "warn" : !w ? "muted" : w.ok ? "" : "bad"}`} data-label="Queues" title={queueTitle(w, !!c.queue_growing)}>
        {queueText(w)}{c.queue_growing ? " ↑" : ""}</td>
      {/* its server's Qwen seconds per UTC day (hub ai_schedule.server_days): its share of the host's shared vLLM */}
      <td className="small qwen-cell" data-label="Qwen" title={qwenTitle(c.qwen)}>{c.qwen ? <><QwenBars q={c.qwen} /> {qwenText(c.qwen)}</> : "—"}</td>
      <td className="small" title={c.last_error ?? undefined}>{PHASE_LABEL[c.phase] ?? c.phase}{c.state === "failed" && c.last_error ? `: ${c.last_error.slice(0, 80)}` : ""}
        {probs.length > 0 && (
          <span className="problem-badges">{probs.map((p) => (
            <span key={p} className={`alert-kind problem-${p}`} title={p === "image" ? imageTitle(c) : undefined}>
              {p === "down" ? downText(c) : PROBLEM_LABEL[p]}</span>))}
          </span>)}</td>
      <td className="acts"><InstanceMenu ci={c} onChanged={onChanged} onNetworks={onNetworks} onResources={onResources} onRemove={onRemove} /></td>
    </tr>
  );
}

/** Sort by … and the order; the order button flips between the two. */
export function SortPick<S extends string>({ sorts, labels, sort, order, onChange }: {
  sorts: readonly S[]; labels: Record<S, string>; sort: string; order: string; onChange: (sort: S, order: "asc" | "desc") => void;
}) {
  return (
    <span className="sort-pick">
      <select value={sort} aria-label="Sort by" onChange={(e) => onChange(e.target.value as S, defaultOrder(e.target.value) as "asc" | "desc")}>
        {sorts.map((s) => <option key={s} value={s}>Sort: {labels[s]}</option>)}
      </select>
      <button type="button" className="ghost small" aria-label={order === "asc" ? "Ascending: show descending" : "Descending: show ascending"}
        title={order === "asc" ? "Ascending" : "Descending"} onClick={() => onChange(sort as S, order === "asc" ? "desc" : "asc")}>{order === "asc" ? "↑" : "↓"}</button>
    </span>
  );
}

export function Pager({ page, shown, total, onPage }: { page: number; shown: number; total: number; onPage: (p: number) => void }) {
  const last = lastPage(total);
  if (last <= 1 && page <= 1) return <p className="muted small pager">{pageText(page, shown, total)}</p>;
  return (
    <div className="row pager small">
      <span className="muted">{pageText(page, shown, total)}</span>
      <button className="ghost small" disabled={page <= 1} onClick={() => onPage(Math.min(page - 1, last))}>‹ Previous</button>
      <button className="ghost small" disabled={page >= last} onClick={() => onPage(page + 1)}>Next ›</button>
    </div>
  );
}

/**
 * The row's ⋯ menu: storage, CPU/memory, camera limit, Site networks, and Remove at the bottom. Arrows move between
 * the items, Escape or a click outside closes it (the focus goes back to the button).
 */
function InstanceMenu({ ci, onChanged, onNetworks, onResources, onRemove }: { ci: CentralInstance; onChanged: () => void; onNetworks: () => void; onResources: () => void; onRemove: () => void }) {
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const btn = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  useDismiss(box, open, () => setOpen(false), btn);
  useEffect(() => { if (open) menu.current?.querySelector<HTMLElement>("[role=menuitem]:not(:disabled)")?.focus(); }, [open]);
  const site = ci.location_name ?? ci.location_id;
  const live = ci.state !== "deleting" && ci.state !== "deleted";
  const offline = ci.host_online === false;
  const item = (f: () => unknown) => () => { setOpen(false); btn.current?.focus(); f(); };
  const storage = async () => {
    const v = await promptDialog(`Storage for ${site} (GB)`, { initial: String(ci.quota_gb), label: "GB",
      message: "The instance's disk quota on its host, at least 10 GB. Below what it uses, it deletes its oldest recordings." });
    if (v == null) return;
    const q = Number(v.trim());
    if (!/^\d+$/.test(v.trim()) || q < 10) { toast.error("The storage quota is a whole number of GB, at least 10"); return; }
    try { await api.updateCentral(ci.location_id, ci.id, { quota_gb: q }); toast.success(`Storage set to ${fmtGB(q)}`); onChanged(); } catch (e) { toast.error(e); }
  };
  // the data dataset (ZFS hosts, agent 0.3.2+): instance_quota_gb null = none of its own (XFS / no quota) or not reported yet
  const hasDataQuota = ci.instance_quota_gb != null;
  const dataStorage = async () => {
    const used = ci.instance_used_gb;
    const v = await promptDialog(`Data storage for ${site} (GB)`, { initial: String(Math.round(ci.instance_quota_gb ?? 50)), label: "GB",
      message: `Its database and event media${used != null ? ` (${fmtGB(used)} used now)` : ""}. The server stops storing events when this is full; `
        + "a quota below what it uses is refused. Raising it takes effect at once." });
    if (v == null) return;
    const q = Number(v.trim());
    if (!/^\d+$/.test(v.trim()) || q < 1) { toast.error("The data storage quota is a whole number of GB, at least 1"); return; }
    try { await api.setCentralDataQuota(ci.location_id, ci.id, { quota_gb: q }); toast.success(`Data storage set to ${fmtGB(q)}`); onChanged(); } catch (e) { toast.error(e); }
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
  const offlineTitle = offline ? "The host is offline" : undefined;
  return (
    <div ref={box} className="menu-anchor">
      <button ref={btn} type="button" className="ghost small" aria-haspopup="menu" aria-expanded={open} aria-label={`Actions for ${site}`} onClick={() => setOpen((o) => !o)}>⋯</button>
      {open && (
        <div ref={menu} className="menu-pop instance-menu-pop" role="menu" aria-label={`Actions for ${site}`} onKeyDown={menuKeys}>
          <button role="menuitem" disabled={!live || offline} title={offlineTitle} onClick={item(storage)}>Change storage…</button>
          <button role="menuitem" disabled={!live || offline || !hasDataQuota}
            title={offlineTitle ?? (hasDataQuota ? "The quota of its database and event media" : "Only for instances with a data quota of their own (ZFS hosts, agent 0.3.2+); here it shares the storage quota")}
            onClick={item(dataStorage)}>Change data storage…</button>
          <button role="menuitem" disabled={!live || offline} title={offlineTitle ?? "CPU and memory limits (the instance restarts)"} onClick={item(onResources)}>Change CPU/memory…</button>
          <button role="menuitem" disabled={!live} onClick={item(limit)}>Change camera limit…</button>
          <button role="menuitem" disabled={!live || offline} title={offlineTitle} onClick={item(onNetworks)}>Site networks…</button>
          <div className="menu-sep" role="separator" />
          <button role="menuitem" className="menu-danger" disabled={ci.state === "deleted"} onClick={item(onRemove)}>Remove…</button>
        </div>)}
    </div>
  );
}

/**
 * A change to several instances at once, listed before it runs: the camera limit (blank = no limit), or storage raised to
 * at least N GB (the ones already there are skipped). Runs one instance after another; the failures are listed after.
 */
function BulkDialog({ action, selected, onClose, onDone }: { action: BulkAction; selected: CentralInstance[]; onClose: () => void; onDone: () => void }) {
  const [value, setValue] = useState(action === "storage" ? String(Math.max(...selected.map((c) => c.quota_gb))) : "");
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState<{ ci: CentralInstance; error: string }[]>([]);
  const lim = action === "limit" ? parseCameraLimit(value) : null;
  const gb = action === "storage" && !bulkValueError(action, value) ? Number(value.trim()) : null;
  const err = action === "limit" ? lim?.error ?? null : bulkValueError(action, value);
  const plan = bulkPlan(action, selected, gb);
  const name = (c: CentralInstance) => c.location_name ?? c.location_id;
  const run = async () => {
    setBusy(true);
    const bad: { ci: CentralInstance; error: string }[] = [];
    for (const c of plan.apply) {
      try { await api.updateCentral(c.location_id, c.id, action === "limit" ? { camera_limit: lim?.value ?? null } : { quota_gb: gb! }); }
      catch (e) { bad.push({ ci: c, error: e instanceof Error ? e.message : String(e) }); }
    }
    setBusy(false);
    const ok = plan.apply.length - bad.length;
    if (ok) toast.success(`${ok} instance${ok === 1 ? "" : "s"} changed`);
    if (bad.length) { setFailed(bad); return; }
    onDone();
  };
  const title = action === "limit" ? "Change the camera limit" : "Raise the storage";
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <form className="modal central-dialog central-form" onClick={(e) => e.stopPropagation()} style={{ maxWidth: 560 }}
        onSubmit={(e) => { e.preventDefault(); if (!err && plan.apply.length && !busy) run(); }}>
        <header className="modal-head"><h2>{title} of {plan.apply.length} instance{plan.apply.length === 1 ? "" : "s"}</h2><button type="button" className="ghost" onClick={onClose} aria-label="Close">✕</button></header>
        {failed.length ? (
          <>
            <p className="small bad">{failed.length} could not be changed:</p>
            <ul className="small bulk-list">{failed.map((f) => <li key={f.ci.id}><strong>{name(f.ci)}</strong>: {f.error.replace(/^\d+ /, "")}</li>)}</ul>
            <div className="row"><button type="button" onClick={onDone}>Done</button></div>
          </>
        ) : (
          <>
            {action === "limit" ? (
              <label className="field"><span>Cameras <span className="muted small">blank = no limit</span></span>
                <input inputMode="numeric" value={value} placeholder="no limit" onChange={(e) => setValue(e.target.value.replace(/[^0-9]/g, ""))} /></label>
            ) : (
              <label className="field"><span>Storage (GB) <span className="muted small">only raised: an instance with as much or more keeps its own</span></span>
                <input inputMode="numeric" value={value} onChange={(e) => setValue(e.target.value.replace(/[^0-9]/g, ""))} /></label>
            )}
            <ul className="small bulk-list">
              {plan.apply.map((c) => (
                <li key={c.id}><strong>{name(c)}</strong> <span className="muted">· {c.org_name ?? c.org_id}{c.host_name ? ` · ${c.host_name}` : ""} · now {action === "limit"
                  ? (c.camera_limit == null ? "no limit" : `limit ${c.camera_limit}`) : fmtGB(c.quota_gb)}</span>
                  {action === "limit" && lim?.value != null && (c.camera_count ?? 0) > lim.value && <span className="warn-text"> · has {c.camera_count}: they keep recording, new ones are refused</span>}</li>))}
              {plan.skip.map((s) => <li key={s.ci.id} className="muted"><s>{name(s.ci)}</s> · left alone: {s.why}</li>)}
            </ul>
            {action === "storage" && <p className="small muted">Each instance's host makes the change; one that refuses (not enough room) keeps its quota and is listed after.</p>}
            <div className="row">
              <button type="submit" disabled={busy || !!err || !plan.apply.length}>{busy ? "Changing…" : `Apply to ${plan.apply.length}`}</button>
              <button type="button" className="ghost" onClick={onClose}>Cancel</button>
              {err && <span className="muted small">{err}</span>}
            </div>
          </>
        )}
      </form>
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
