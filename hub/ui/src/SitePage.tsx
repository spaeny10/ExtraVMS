/**
 * One Site: header with its rollup, tabs Live · Timeline · Find · Alerts · Servers · Settings, and the server panel at
 * /sites/:id/servers/:serverId. Live is the combined grid (SiteLive), Find and Alerts are the customer-wide pages scoped
 * to this Site (FindPage, AlertsPage's SiteAlerts).
 */
import { useCallback, useEffect, useState } from "react";
import { Icon, confirmDialog, promptDialog, toast } from "@site/ui";
import { type Camera, type Me, type Org, type Server, type Site, ago, api, fmtTime } from "./api";
import { isAdmin, ofTotal } from "./access";
import { Breadcrumbs } from "./Breadcrumbs";
import { SITE_TABS, type SiteTab, consoleHref, go, navigate, serverHref, siteHref } from "./nav";
import { ServerActions, ServerCard } from "./servers";
import { SiteLive } from "./SiteLive";
import { SiteTimeline } from "./SiteTimeline";
import { FindPage } from "./FindPage";
import { SiteAlerts } from "./AlertsPage";

const TAB_LABEL: Record<SiteTab, [string, string]> = {
  live: ["Live", "live"], timeline: ["Timeline", "timeline"], find: ["Find", "find"], alerts: ["Alerts", "alert"], servers: ["Servers", "grid"], settings: ["Settings", "settings"],
};

/** The Site with its servers (retired ones included, marked), refreshed every 15 s like the Sites list. */
export function useSite(siteId: string) {
  const [site, setSite] = useState<Site | null>(null);
  const [error, setError] = useState<string | null>(null);
  const reload = useCallback(() => api.location(siteId, true).then((s) => { setSite(s); setError(null); })
    .catch((e: Error) => setError(e.message.startsWith("404") ? "This site doesn't exist (any more)." : e.message.startsWith("403") ? "You don't have access to this site." : e.message)), [siteId]);
  useEffect(() => { setSite(null); reload(); const t = setInterval(reload, 15000); return () => clearInterval(t); }, [reload]);
  return { site, error, reload };
}

export function SitePage({ org, me, siteId, tab, serverId, onOrg }: { org: Org; me: Me; siteId: string; tab: string; serverId?: string; onOrg: (id: string) => void }) {
  const { site, error, reload } = useSite(siteId);
  // a link into another customer's Site switches the header's Customer picker to it
  useEffect(() => { if (site && site.org_id !== org.id && me.orgs.some((o) => o.id === site.org_id)) onOrg(site.org_id); }, [site, org.id, me.orgs, onOrg]);
  if (error) return <p className="muted">{error} <a href="/sites" onClick={go("/sites")}>All sites</a></p>;
  if (!site) return <p className="muted">Loading…</p>;
  const siteOrg = me.orgs.find((o) => o.id === site.org_id) ?? org;
  const admin = isAdmin(siteOrg, me);
  const active = site.servers.filter((s) => !s.retired_at);
  const server = serverId ? site.servers.find((s) => s.id === serverId) : undefined;
  return (
    <>
      <Breadcrumbs org={siteOrg} site={site} server={server} />
      <SiteHeader site={site} servers={active} admin={admin} onChanged={reload} />
      <div className="segmented site-tabs" role="tablist">
        {SITE_TABS.map((t) => (
          <button key={t} role="tab" aria-selected={tab === t} className={tab === t ? "active" : ""} onClick={() => navigate(siteHref(site.id, t))}>
            <Icon name={TAB_LABEL[t][1]} size={16} /> {TAB_LABEL[t][0]}
          </button>
        ))}
      </div>
      {serverId ? (server ? <ServerPanel site={site} server={server} admin={admin} onChanged={reload} /> : <p className="muted">That server isn't in this site. <a href={siteHref(site.id, "servers")} onClick={go(siteHref(site.id, "servers"))}>Servers</a></p>)
        : tab === "live" ? <SiteLive org={siteOrg} site={site} />
        : tab === "timeline" ? <SiteTimeline org={siteOrg} site={site} query={location.search} />
        : tab === "find" ? <FindPage org={siteOrg} site={site} />
        : tab === "alerts" ? <SiteAlerts org={siteOrg} site={site} />
        : tab === "servers" ? <ServersTab site={site} admin={admin} onChanged={reload} />
        : <SettingsTab site={site} admin={admin} onChanged={reload} />}
    </>
  );
}

function SiteHeader({ site, servers, admin, onChanged }: { site: Site; servers: Server[]; admin: boolean; onChanged: () => void }) {
  const [menu, setMenu] = useState(false);
  const save = async (b: { name?: string; address?: string }) => { try { await api.updateLocation(site.id, b); onChanged(); } catch (e) { toast.error(e); } };
  return (
    <div className="site-head">
      <div>
        <h2>{site.name}</h2>
        {site.address && <div className="muted small">{site.address}</div>}
      </div>
      <div className="row site-chips">
        <span className={`chip ${site.servers_online < site.servers_total ? "warn" : ""}`}>Servers {ofTotal(site.servers_online, site.servers_total, "online")}</span>
        <span className={`chip ${site.cameras_online < site.cameras_total ? "warn" : ""}`}>Cameras {ofTotal(site.cameras_online, site.cameras_total, "up")}</span>
        {site.open_alerts > 0 && <span className="chip warn">⚠ {site.open_alerts} open alert{site.open_alerts > 1 ? "s" : ""}</span>}
      </div>
      <span className="spacer" />
      <div className="menu-anchor">
        <button className="ghost" aria-label="More" aria-expanded={menu} onClick={() => setMenu(!menu)}><Icon name="more" /></button>
        {menu && (
          <>
            <div className="menu-veil" onClick={() => setMenu(false)} />
            <div className="menu-pop" role="menu" onClick={() => setMenu(false)}>
              {admin && <button role="menuitem" onClick={async () => { const name = await promptDialog("Rename site", { initial: site.name, label: "Name" }); if (name?.trim()) await save({ name: name.trim() }); }}>Rename…</button>}
              {admin && <button role="menuitem" onClick={async () => { const address = await promptDialog("Site address", { initial: site.address, label: "Address" }); if (address != null) await save({ address: address.trim() }); }}>Address…</button>}
              {servers.length > 0 && <div className="menu-label">Open server console ▸</div>}
              {servers.map((s) => <a key={s.id} role="menuitem" href={consoleHref(s.id)}>{s.name} {s.online ? "" : <span className="muted">(offline)</span>}</a>)}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

/** Interim tab body: what is coming here, and each server's own page for it today. */

function ServersTab({ site, admin, onChanged }: { site: Site; admin: boolean; onChanged: () => void }) {
  const [sites, setSites] = useState<Site[]>([]);
  useEffect(() => { if (admin) api.locations(site.org_id).then(setSites).catch(() => setSites([])); }, [admin, site.org_id]);
  const now = Date.now() / 1000;
  if (site.servers.length === 0) return <p className="muted">No servers in this site yet.{admin ? <> Enrol one under <a href="/customer/servers" onClick={go("/customer/servers")}>Customer → Servers</a>.</> : ""}</p>;
  return (
    <div className="site-grid">
      {site.servers.map((s) => (
        <ServerCard key={s.id} s={s} now={now} href={serverHref(site.id, s.id)}>
          <ServerActions s={s} admin={admin} sites={sites} onChanged={onChanged} />
        </ServerCard>
      ))}
    </div>
  );
}

const ZONES: string[] = typeof Intl.supportedValuesOf === "function" ? Intl.supportedValuesOf("timeZone") : [];

function SettingsTab({ site, admin, onChanged }: { site: Site; admin: boolean; onChanged: () => void }) {
  const [f, setF] = useState({ name: site.name, address: site.address ?? "", timezone: site.timezone ?? "", notes: site.notes ?? "" });
  useEffect(() => { setF({ name: site.name, address: site.address ?? "", timezone: site.timezone ?? "", notes: site.notes ?? "" }); }, [site.id]); // eslint-disable-line react-hooks/exhaustive-deps
  const dirty = f.name !== site.name || f.address !== (site.address ?? "") || f.timezone !== (site.timezone ?? "") || f.notes !== (site.notes ?? "");
  // the backend counts retired servers too: they still belong to the site until moved or removed
  const remaining = site.servers_total + site.retired_servers;
  const save = async () => {
    try { await api.updateLocation(site.id, { name: f.name.trim(), address: f.address.trim(), timezone: f.timezone.trim() || null, notes: f.notes.trim() || null }); toast.success("Saved"); onChanged(); }
    catch (e) { toast.error(e); }
  };
  const del = async () => {
    if (!(await confirmDialog(`Delete site ${site.name}?`, { message: "People who could see only this site lose access to it. This cannot be undone.", confirmLabel: "Delete", danger: true }))) return;
    try { await api.deleteLocation(site.id); toast.success(`${site.name} deleted`); navigate("/sites"); } catch (e) { toast.error(e); }
  };
  return (
    <div className="card site-settings">
      <label className="field"><span>Name</span><input value={f.name} disabled={!admin} maxLength={120} onChange={(e) => setF({ ...f, name: e.target.value })} /></label>
      <label className="field"><span>Address</span><input value={f.address} disabled={!admin} maxLength={200} onChange={(e) => setF({ ...f, address: e.target.value })} /></label>
      <label className="field"><span>Time zone</span><input value={f.timezone} disabled={!admin} list="tz-list" placeholder="e.g. America/Chicago" onChange={(e) => setF({ ...f, timezone: e.target.value })} /></label>
      <datalist id="tz-list">{ZONES.map((z) => <option key={z} value={z} />)}</datalist>
      <label className="field"><span>Notes</span><textarea value={f.notes} disabled={!admin} maxLength={2000} rows={4} onChange={(e) => setF({ ...f, notes: e.target.value })} /></label>
      {admin && (
        <div className="row">
          <button disabled={!dirty || !f.name.trim()} onClick={save}>Save</button>
          <span className="spacer" />
          <button className="ghost small" disabled={remaining > 0} onClick={del}>Delete site</button>
          {remaining > 0 && <span className="muted small">Move or remove its {remaining} server{remaining > 1 ? "s" : ""} first (Servers tab).</span>}
        </div>
      )}
      <p className="muted small" style={{ marginBottom: 0 }}>Created {fmtTime(site.created_at)} · updated {ago(site.updated_at)}</p>
    </div>
  );
}

/** /sites/:id/servers/:serverId — one server's status and cameras (from the hub's registry, so it works while offline). */
function ServerPanel({ site, server, admin, onChanged }: { site: Site; server: Server; admin: boolean; onChanged: () => void }) {
  const [cams, setCams] = useState<Camera[] | null>(null);
  const [sites, setSites] = useState<Site[]>([]);
  useEffect(() => { api.locationCameras(site.id).then((c) => setCams(c.filter((x) => x.server_id === server.id))).catch(() => setCams([])); }, [site.id, server.id, server.last_seen_at]);
  useEffect(() => { if (admin) api.locations(site.org_id).then(setSites).catch(() => setSites([])); }, [admin, site.org_id]);
  const sm = server.summary ?? {};
  return (
    <>
      <div className="card">
        <div className="row">
          <span className={`dot ${server.online ? "ok" : ""}`} />
          <h3 style={{ margin: 0 }}>{server.name}</h3>
          {server.retired_at ? <span className="alert-kind">retired</span> : null}
          <span className="muted small">{server.online ? "online" : `offline · last seen ${ago(server.last_seen_at)}`}</span>
        </div>
        <div className="stats-grid small">
          <div><span className="muted">Host</span> {server.hostname ?? "—"}</div>
          <div><span className="muted">Version</span> {server.version ?? "—"}</div>
          <div><span className="muted">Disk</span> {sm.disk ? `${sm.disk.free_gb.toLocaleString()} of ${sm.disk.total_gb.toLocaleString()} GB free` : "—"}</div>
          <div><span className="muted">Clock</span> {server.clock_skew_s != null ? `${server.clock_skew_s > 0 ? "+" : ""}${Math.round(server.clock_skew_s)} s` : "—"}</div>
          {server.location && <div><span className="muted">Note</span> {server.location}</div>}
        </div>
        <div className="row server-actions"><ServerActions s={server} admin={admin} sites={sites} onChanged={onChanged} /></div>
      </div>
      <div className="card">
        <h3 style={{ marginTop: 0 }}>Cameras</h3>
        {cams === null ? <p className="muted small">Loading…</p> : cams.length === 0 ? <p className="muted small">None reported yet.</p> : (
          <table className="hub-table">
            <thead><tr><th>Camera</th><th>Status</th><th>Stream</th><th>Problems</th><th /></tr></thead>
            <tbody>{cams.map((c) => (
              <tr key={c.camera_id} className={c.enabled ? "" : "muted"}>
                <td>{c.name}</td>
                <td>{!c.enabled ? "disabled" : c.missing_since ? `missing since ${fmtTime(c.missing_since)}` : c.online ? "up" : server.online ? "no stream" : "server offline"}</td>
                <td>{c.bitrate_mbps != null ? `${c.bitrate_mbps} Mbps` : "—"}</td>
                <td className="small">{(c.problems ?? []).join("; ") || "—"}</td>
                <td><a className="small" href={consoleHref(server.id, "live")}>Live ↗</a></td>
              </tr>))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
