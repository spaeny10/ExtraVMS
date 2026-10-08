/**
 * One Site: header with its rollup, tabs Live · Timeline · Find · Ask · Alerts · Servers · Settings, and the server panel
 * at /sites/:id/servers/:serverId. Live is the combined grid (SiteLive), Find the server UI's Find over the Site's
 * servers (SiteFind: search and filters), Ask one answer for the whole Site in private conversations (site/SiteAsk,
 * /sites/:id/ask/:thread), Alerts the customer-wide page scoped to this Site (AlertsPage's SiteAlerts).
 */
import { useCallback, useEffect, useState } from "react";
import { Icon, confirmDialog, promptDialog, toast } from "@site/ui";
import { type Camera, type Me, type Org, type Server, type Site, type SiteCoverage, ago, api, fmtTime } from "./api";
import { canCheckCoverage, canEditMonitoring, isAdmin, isSocUser, ofTotal } from "./access";
import { Breadcrumbs } from "./Breadcrumbs";
import { DirectChips } from "./DirectChip";
import { SETTINGS_SECTIONS, SITE_TABS, type SettingsSection, type SiteTab, consoleHref, go, navigate, serverHref, settingsHref, siteHref } from "./nav";
import { AddressBox } from "./site/AddressBox";
import { CentralDetails, CentralPlaceholder, CentralStats, useCentral } from "./site/CentralCard";
import { COVERAGE_ANCHOR, CoverageCard } from "./site/CoverageCard";
import { bestCarrier, chipText } from "./coverage";
import { datacenterLine, instanceFor, siteBandwidth, unenrolledInstances, uploadLine } from "./central";
import { ContactsBox } from "./site/ContactsBox";
import { MonitoringBox } from "./site/MonitoringBox";
import { ProceduresBox } from "./site/ProceduresBox";
import { SiteIncidents } from "./site/SiteIncidents";
import { ServerActions, ServerCard } from "./servers";
import { SiteLive } from "./SiteLive";
import { SiteTimeline } from "./SiteTimeline";
import { SiteFind } from "./SiteFind";
import { SiteAsk } from "./site/SiteAsk";
import { SiteAlerts } from "./AlertsPage";
import { useTabStrip } from "./tabStrip";
import { type PlaceForm, cityState, fmtCoord, hasPoint, placePatch } from "./place";

const TAB_LABEL: Record<SiteTab, [string, string]> = {
  live: ["Live", "live"], timeline: ["Timeline", "timeline"], find: ["Find", "find"], ask: ["Ask", "sparkle"], alerts: ["Alerts", "alert"], servers: ["Servers", "grid"], settings: ["Settings", "settings"],
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

/**
 * The Site's cellular coverage (hub coverage.py) when this user sees coverage at all (me.coverage.visible; trial = hub
 * administrators only). Loaded once per Site page and again when the Site's point changes; shared by the header chip and
 * Settings › General (the coverage card under the map, and the map's rings).
 */
function useCoverage(site: Site | null, me: Me) {
  const on = !!me.coverage?.visible && !!site;
  const [cov, setCov] = useState<SiteCoverage | null>(null);
  const key = site ? `${site.id}|${site.lat ?? ""}|${site.lon ?? ""}|${site.address}` : "";
  useEffect(() => {
    if (!on || !site) { setCov(null); return; }
    let live = true;
    api.coverage(site.id).then((c) => { if (live) setCov(c); }).catch(() => { if (live) setCov(null); });
    return () => { live = false; };
  }, [on, key]); // eslint-disable-line react-hooks/exhaustive-deps
  return { cov, setCov };
}

export function SitePage({ org, me, siteId, tab, section = "general", serverId, threadId, onOrg }: {
  org: Org; me: Me; siteId: string; tab: string; section?: SettingsSection; serverId?: string; threadId?: string; onOrg: (id: string) => void;
}) {
  const { site, error, reload } = useSite(siteId);
  const { cov, setCov } = useCoverage(site, me);
  // phones: the seven tabs scroll sideways, the current one kept in view (tabStrip.ts, hub.css .scroll-tabs)
  const strip = useTabStrip(tab);
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
      <SiteHeader org={siteOrg} site={site} server={server} servers={active} admin={admin} onChanged={reload} cov={cov} />
      {/* styled like the server UI's top nav (plain buttons, the current one raised) so a Site reads like a server */}
      <div ref={strip.ref} className={`site-tabs ${strip.className}`} role="tablist">
        {SITE_TABS.map((t) => (
          <button key={t} role="tab" aria-selected={tab === t} className={tab === t ? "active" : ""} onClick={() => navigate(siteHref(site.id, t))}>
            <Icon name={TAB_LABEL[t][1]} size={16} /> {TAB_LABEL[t][0]}
          </button>
        ))}
      </div>
      {serverId ? (server ? <ServerPanel site={site} server={server} admin={admin} onChanged={reload} /> : <p className="muted">That server isn't in this site. <a href={siteHref(site.id, "servers")} onClick={go(siteHref(site.id, "servers"))}>Servers</a></p>)
        : tab === "live" ? <SiteLive org={siteOrg} site={site} />
        : tab === "timeline" ? <SiteTimeline org={siteOrg} site={site} query={location.search} canRecoverSd={admin} />
        : tab === "find" ? <SiteFind org={siteOrg} site={site} />
        : tab === "ask" ? <SiteAsk site={site} threadId={threadId} />
        : tab === "alerts" ? <><SiteIncidents site={site} /><SiteAlerts org={siteOrg} site={site} /></>
        : tab === "servers" ? <ServersTab site={site} admin={admin} onChanged={reload} />
        : <SettingsSections site={site} org={siteOrg} me={me} section={section} admin={admin} onChanged={reload} cov={cov} setCov={setCov} />}
    </>
  );
}

/**
 * One row: "Customer › Site" as the title (the customer links back to its Sites list), the rollup chips, the ⋯ menu.
 * It replaces a crumb line, a title block and a chip row, so the Live grid starts where the server UI's does.
 */
function SiteHeader({ org, site, server, servers, admin, onChanged, cov }: {
  org: Org; site: Site; server?: Server; servers: Server[]; admin: boolean; onChanged: () => void; cov: SiteCoverage | null;
}) {
  const [menu, setMenu] = useState(false);
  const save = async (b: { name?: string; address?: string }) => { try { await api.updateLocation(site.id, b); onChanged(); } catch (e) { toast.error(e); } };
  return (
    <div className="site-head">
      <Breadcrumbs org={org} site={site} server={server} title={site.address || undefined} />
      {site.address && <span className="muted small site-addr">{site.address}</span>}
      {/* where it is: the map, coordinates and links are on Settings › General (read-only there for viewers) */}
      <a className={`chip small place-chip ${hasPoint(site) ? "" : "muted"}`} href={settingsHref(site.id, "general")} onClick={go(settingsHref(site.id, "general"))}
        title={hasPoint(site) ? `${cityState(site.address_parts) || site.address} · ${fmtCoord(site.lat, site.lon)} · map in Settings` : "Not on the map yet: set the address in Settings"}>
        📍{hasPoint(site) ? " Map" : " Locate"}
      </a>
      <div className="site-chips">
        <span className={`chip ${site.servers_online < site.servers_total ? "warn" : ""}`}>Servers {ofTotal(site.servers_online, site.servers_total, "online")}</span>
        <span className={`chip ${site.cameras_online < site.cameras_total ? "warn" : ""}`}>Cameras {ofTotal(site.cameras_online, site.cameras_total, "up")}</span>
        {site.open_alerts > 0 && <span className="chip warn">⚠ {site.open_alerts} open alert{site.open_alerts > 1 ? "s" : ""}</span>}
        <DirectChips servers={servers} />
        <CoverageChip site={site} cov={cov} />
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

/** "📶 VZW 8.6": the best carrier's overall score, linking to the coverage under the map on Settings › General. */
function CoverageChip({ site, cov }: { site: Site; cov: SiteCoverage | null }) {
  const text = chipText(cov?.data);
  const best = bestCarrier(cov?.data);
  if (!cov || !text || !best) return null;
  const href = settingsHref(site.id, "general");
  const open = (e: React.MouseEvent) => {
    e.preventDefault();
    navigate(href);
    // the map loads lazily above it: scroll once it has had a moment to take its height
    setTimeout(() => document.getElementById(COVERAGE_ANCHOR)?.scrollIntoView({ behavior: "smooth", block: "start" }), 250);
  };
  return (
    <a className={`chip cov-chip ${cov.evaluation ? "eval" : ""}`} href={href} onClick={open}
      title={`Cellular: best here ${best.name} ${best.technology.toUpperCase()}, overall ${best.score.toFixed(1)} of 10 (CoverageMap)${cov.evaluation ? " · evaluation only, hub administrators" : ""}`}>
      {text}
    </a>
  );
}

/**
 * The Site's servers as cards. A central recording instance is its own server's card (the Site's admins and hub
 * administrators see its connection, limit, camera addresses and Peplink sheet there; site/CentralCard.tsx); one whose
 * server has not enrolled yet is a placeholder card with its progress.
 */
function ServersTab({ site, admin, onChanged }: { site: Site; admin: boolean; onChanged: () => void }) {
  const [sites, setSites] = useState<Site[]>([]);
  useEffect(() => { if (admin) api.locations(site.org_id).then(setSites).catch(() => setSites([])); }, [admin, site.org_id]);
  const central = useCentral(site.id, admin, onChanged);
  const hubAdmin = !!central?.can_manage;
  const pending = unenrolledInstances(central?.instances, site.servers);
  const now = Date.now() / 1000;
  // what the Site's servers pull from its cameras (a central instance: the site's cellular upload)
  const upload = uploadLine(siteBandwidth(site.servers));
  return (
    <>
      {upload && <p className="small site-upload">{upload}</p>}
      {site.servers.length === 0 && pending.length === 0 ? <p className="muted">No servers in this site yet.{admin ? <> Enroll one under <a href="/customer/servers" onClick={go("/customer/servers")}>Customer → Servers</a>.</> : ""}</p> : (
        <div className="site-grid">
          {site.servers.map((s) => {
            const ci = instanceFor(central?.instances, s.id);
            return (
              <ServerCard key={s.id} s={s} now={now} href={serverHref(site.id, s.id)}
                central={ci ? { line: datacenterLine(ci, hubAdmin), stats: <CentralStats ci={ci} />, details: <CentralDetails siteName={site.name} ci={ci} hubAdmin={hubAdmin} enrolled /> } : undefined}>
                <ServerActions s={s} admin={admin} sites={sites} onChanged={onChanged} />
              </ServerCard>
            );
          })}
          {pending.map((ci) => <CentralPlaceholder key={ci.id} siteName={site.name} ci={ci} hubAdmin={hubAdmin} />)}
        </div>
      )}
    </>
  );
}

const SECTION_LABEL: Record<SettingsSection, string> = { general: "General", monitoring: "Monitoring", contacts: "Contacts", procedures: "Procedures" };

/**
 * Settings sub-tabs. Monitoring is shown to everyone (read-only: is the SOC watching now, and until when); its
 * editor, Contacts and Procedures are for customer admins and SOC supervisors (canEditMonitoring). Arm/disarm now is
 * for customer operators and up, and SOC staff. A section someone may not open falls back to General.
 */
function SettingsSections({ site, org, me, section, admin, onChanged, cov, setCov }: {
  site: Site; org: Org; me: Me; section: SettingsSection; admin: boolean; onChanged: () => void; cov: SiteCoverage | null; setCov: (c: SiteCoverage) => void;
}) {
  const editor = canEditMonitoring(org, me);
  const canArm = editor || isSocUser(me) || org.role === "operator";
  const allowed = SETTINGS_SECTIONS.filter((s) => editor || s === "general" || s === "monitoring");
  const cur = allowed.includes(section) ? section : "general";
  const strip = useTabStrip(cur);
  return (
    <>
      <div ref={strip.ref} className={`segmented settings-tabs ${strip.className}`} role="tablist">
        {allowed.map((s) => (
          <button key={s} role="tab" aria-selected={cur === s} className={cur === s ? "active" : ""} onClick={() => navigate(settingsHref(site.id, s))}>{SECTION_LABEL[s]}</button>
        ))}
      </div>
      {cur === "general" ? <SettingsTab site={site} admin={admin} onChanged={onChanged} me={me} org={org} cov={cov} setCov={setCov} />
        : cur === "monitoring" ? <MonitoringBox key={site.id} site={site} canEdit={editor} canArm={canArm} />
        : cur === "contacts" ? <ContactsBox key={site.id} site={site} />
        : <ProceduresBox key={site.id} site={site} />}
    </>
  );
}

const ZONES: string[] = typeof Intl.supportedValuesOf === "function" ? Intl.supportedValuesOf("timeZone") : [];

type SettingsForm = PlaceForm & { name: string; notes: string };
const formOf = (site: Site): SettingsForm => ({
  name: site.name, notes: site.notes ?? "", address: site.address ?? "", timezone: site.timezone ?? "",
  lat: site.lat ?? null, lon: site.lon ?? null, address_parts: site.address_parts ?? null, source: site.geocode_source ?? null, autoTz: null,
});

function SettingsTab({ site, admin, onChanged, me, org, cov, setCov }: {
  site: Site; admin: boolean; onChanged: () => void; me: Me; org: Org; cov: SiteCoverage | null; setCov: (c: SiteCoverage) => void;
}) {
  const [f, setF] = useState<SettingsForm>(() => formOf(site));
  useEffect(() => { setF(formOf(site)); }, [site.id]); // eslint-disable-line react-hooks/exhaustive-deps
  // a time zone that is exactly the one at the saved point counts as "set by the address": a new pick may replace it
  useEffect(() => {
    if (site.lat == null || site.lon == null || !site.timezone) return;
    api.geocodeTimezone(site.lat, site.lon).then((r) => { if (r.timezone && r.timezone === site.timezone) setF((x) => (x.timezone === r.timezone ? { ...x, autoTz: r.timezone } : x)); }).catch(() => {});
  }, [site.id, site.lat, site.lon, site.timezone]);
  const place = placePatch(f, site);
  const dirty = f.name !== site.name || f.address !== (site.address ?? "") || f.timezone !== (site.timezone ?? "") || f.notes !== (site.notes ?? "") || Object.keys(place).length > 0;
  // the backend counts retired servers too: they still belong to the site until moved or removed
  const remaining = site.servers_total + site.retired_servers;
  const save = async () => {
    try {
      const saved = await api.updateLocation(site.id, { name: f.name.trim(), address: f.address.trim(), timezone: f.timezone.trim() || null, notes: f.notes.trim() || null, ...place });
      setF((x) => ({ ...formOf(saved), autoTz: saved.timezone && saved.timezone === x.autoTz ? x.autoTz : null }));
      toast.success(saved.lat == null && saved.address ? "Saved. The hub will try to put this address on the map." : "Saved");
      onChanged();
    }
    catch (e) { toast.error(e); }
  };
  const del = async () => {
    if (!(await confirmDialog(`Delete site ${site.name}?`, { message: "People who could see only this site lose access to it. This cannot be undone.", confirmLabel: "Delete", danger: true }))) return;
    try { await api.deleteLocation(site.id); toast.success(`${site.name} deleted`); navigate("/sites"); } catch (e) { toast.error(e); }
  };
  return (
    // the saved point's cellular coverage (for those who see coverage: useCoverage) beside the address and map, below on narrower screens
    <div className="settings-general">
      <div className="card site-settings">
        <label className="field"><span>Name</span><input value={f.name} disabled={!admin} maxLength={120} onChange={(e) => setF({ ...f, name: e.target.value })} /></label>
        <AddressBox f={f} setF={(p) => setF((x) => ({ ...x, ...p }))} editable={admin} name={f.name || site.name}
          coverage={cov?.data && cov.basis === "point" && site.lat != null && site.lon != null ? { data: cov.data, lat: site.lat, lon: site.lon } : null}
          check={canCheckCoverage(me, org) ? { orgId: site.org_id, cost: me.coverage?.cost_per_lookup ?? 4, evaluation: !!me.coverage?.evaluation } : null} />
        <label className="field"><span>Time zone</span><input value={f.timezone} disabled={!admin} list="tz-list" placeholder="e.g. America/Chicago (set from the address)" onChange={(e) => setF({ ...f, timezone: e.target.value })} /></label>
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
      {cov && <CoverageCard site={site} cov={cov} onChanged={setCov} />}
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
          <table className="hub-table stack">
            <thead><tr><th>Camera</th><th>Status</th><th>Stream</th><th>Problems</th><th /></tr></thead>
            <tbody>{cams.map((c) => (
              <tr key={c.camera_id} className={c.enabled ? "" : "muted"}>
                <td className="lead">{c.name}</td>
                <td>{!c.enabled ? "disabled" : c.missing_since ? `missing since ${fmtTime(c.missing_since)}` : c.online ? "up" : server.online ? "no stream" : "server offline"}</td>
                <td>{c.bitrate_mbps != null ? `${c.bitrate_mbps} Mbps` : "—"}</td>
                <td className="small wide">{(c.problems ?? []).join("; ") || "—"}</td>
                <td className="acts"><a className="small" href={consoleHref(server.id, "live")}>Live ↗</a></td>
              </tr>))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
