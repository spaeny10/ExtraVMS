/**
 * Sites: one card per Site of the current customer (rollup of its servers and cameras), then servers that belong to
 * no Site the user can see. A card opens the Site page; the server dots say which box is down without opening it.
 */
import { useCallback, useEffect, useState } from "react";
import { toast } from "@site/ui";
import { type Digest, type DigestPart, type Fleet, type Me, type Org, type Server, type Site, ago, api } from "./api";
import { digestPartsFor, isAdmin, lastEventBySite, ofTotal } from "./access";
import { go, navigate, settingsHref, siteHref } from "./nav";
import { SiteMap } from "./map/LazyMap";
import { PIN_COLOUR, type PinStatus, cityState, hasPoint, pinColour, pinStatus } from "./place";
import { ServerCard } from "./servers";

export function SitesPage({ org, me }: { org: Org; me: Me }) {
  const [fleet, setFleet] = useState<Fleet | null>(null);
  const [showRetired, setShowRetired] = useState(false);
  const [mapView, setMapViewState] = useState(() => { try { return localStorage.getItem("hub.sites.map") === "1"; } catch { return false; } });
  const setMapView = (v: boolean) => { setMapViewState(v); try { localStorage.setItem("hub.sites.map", v ? "1" : "0"); } catch { /* private mode */ } };
  const [events, setEvents] = useState<{ site_id: string; start_ts: number }[]>([]);
  const load = useCallback(() => api.fleet(org.id, showRetired).then(setFleet).catch((e) => toast.error(e)), [org.id, showRetired]);
  useEffect(() => { load(); const t = setInterval(load, 15000); return () => clearInterval(t); }, [load]);
  // "last event" fans out to every server, so it refreshes slower than the status cards; mapped to Sites at render
  // time so a server moved between Sites counts for its new one
  useEffect(() => {
    const pull = () => api.fleetEvents(org.id, { limit: 200 }).then((r) => setEvents(r.events)).catch(() => {});
    pull();
    const t = setInterval(pull, 60000);
    return () => clearInterval(t);
  }, [org.id]);
  const g = fleet?.orgs.find((o) => o.org.id === org.id);
  if (!fleet) return null;
  if (!g) return <p className="muted">Nothing to show for {org.name}.</p>;
  // an older hub has no `locations`: every server shows as unassigned rather than nothing at all
  const sites = g.locations ?? [];
  const unassigned = g.unassigned ?? (g.locations ? [] : g.sites);
  const live = g.sites.filter((s) => !s.retired_at);
  const lastEvent = lastEventBySite(events, g.sites);
  return (
    <>
      <h2>Sites <span className="muted small">{org.name} · {live.filter((s) => s.online).length} of {live.length} servers online{g.open_alerts ? ` · ${g.open_alerts} open alerts` : ""}</span>
        {(g.retired ?? 0) > 0 && <label className="small muted" style={{ marginLeft: 12, fontWeight: 400 }}><input type="checkbox" checked={showRetired} onChange={(e) => setShowRetired(e.target.checked)} /> Show retired ({g.retired})</label>}
        {sites.length > 0 && <label className="small muted" style={{ marginLeft: 12, fontWeight: 400 }}><input type="checkbox" checked={mapView} onChange={(e) => setMapView(e.target.checked)} /> Map</label>}</h2>
      {/* customer-wide Find and Alerts, reachable from the hierarchy too: the top nav hides them inside a Site */}
      <div className="row sites-links">
        <a className="link-btn" href="/find" onClick={go("/find")}>Find across all sites</a>
        <a className="link-btn" href="/alerts" onClick={go("/alerts")}>All alerts</a>
      </div>
      {sites.length === 0 && unassigned.length === 0 && (
        <p className="muted">No sites yet. {isAdmin(org, me) ? <>Create one and enrol a server under <a href="/customer/sites" onClick={go("/customer/sites")}>Customer → Sites</a>.</> : "Ask an admin to add one."}</p>
      )}
      {mapView && sites.length > 0 ? <SitesMap sites={sites} />
        : <div className="site-grid">{sites.map((s) => <SiteCard key={s.id} s={s} lastEvent={lastEvent[s.id]} now={fleet.now} />)}</div>}
      {unassigned.length > 0 && (
        <>
          <h3>Unassigned servers <span className="muted small">not in any site{isAdmin(org, me) ? " · move them under Customer → Servers" : ""}</span></h3>
          <div className="site-grid">{unassigned.map((s) => <ServerCard key={s.id} s={s} now={fleet.now} />)}</div>
        </>
      )}
      <DigestCard org={org} />
    </>
  );
}

const PIN_LABEL: Record<PinStatus, string> = { online: "All online", offline: "Server offline", alerts: "Open alerts", empty: "No servers" };

/** Every Site of the customer as a pin coloured by status; a pin opens the Site's Live page. Unlocated Sites listed below. */
function SitesMap({ sites }: { sites: Site[] }) {
  const located = sites.filter((s): s is Site & { lat: number; lon: number } => hasPoint(s));
  const missing = sites.filter((s) => !hasPoint(s));
  const pins = located.map((s) => ({ id: s.id, lat: s.lat, lon: s.lon, colour: pinColour(s), title: `${s.name} · ${PIN_LABEL[pinStatus(s)]}` }));
  return (
    <div className="card sites-map">
      {located.length > 0 ? <SiteMap pins={pins} height={420} onPinClick={(p) => navigate(siteHref(p.id, "live"))} label="Map of the sites" />
        : <p className="muted">None of these sites is on the map yet: set each one's address under its Settings › General.</p>}
      <div className="row map-legend small">
        {(Object.keys(PIN_LABEL) as PinStatus[]).map((k) => <span key={k}><span className="map-legend-dot" style={{ background: PIN_COLOUR[k] }} /> {PIN_LABEL[k]}</span>)}
      </div>
      {missing.length > 0 && (
        <div className="small not-located">
          <span className="muted">Not located yet: </span>
          {missing.map((s, n) => <span key={s.id}>{n > 0 && ", "}<a href={settingsHref(s.id, "general")} onClick={go(settingsHref(s.id, "general"))}>{s.name}</a></span>)}
        </div>
      )}
    </div>
  );
}

/**
 * `onOpen` runs before the in-app navigation (the All customers page switches the active customer first);
 * `noEvents` hides "Last event" where it isn't fetched (it fans out to every server, too much across all customers).
 */
export function SiteCard({ s, lastEvent, now, onOpen, noEvents }: { s: Site; lastEvent?: number; now: number; onOpen?: () => void; noEvents?: boolean }) {
  const href = siteHref(s.id);
  const down = s.servers_total > 0 && s.servers_online < s.servers_total;
  const open = go(href);
  return (
    <a className={`site-card ${s.servers_total && !s.servers_online ? "offline" : ""}`} href={href}
      onClick={(e) => { if (onOpen && e.button === 0 && !e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey) onOpen(); open(e); }}>
      <div className="head">
        <strong>{s.name}</strong>
        <span className="spacer" />
        <span className="server-dots">{s.servers.map((v) => <ServerDot key={v.id} v={v} now={now} />)}</span>
      </div>
      {s.address ? <div className="loc">{s.address}</div> : cityState(s.address_parts) && <div className="loc">{cityState(s.address_parts)}</div>}
      <div className="stats">
        <div><span>Servers</span> {ofTotal(s.servers_online, s.servers_total, "online")}</div>
        <div><span>Cameras</span> {ofTotal(s.cameras_online, s.cameras_total, "up")}</div>
        {!noEvents && <div><span>Last event</span> {lastEvent ? ago(lastEvent, now) : "—"}</div>}
        <div><span>Retired</span> {s.retired_servers || "—"}</div>
      </div>
      {down && <div className="alerts">{s.servers_total - s.servers_online} server{s.servers_total - s.servers_online > 1 ? "s" : ""} offline</div>}
      {s.open_alerts > 0 && <div className="alerts">⚠ {s.open_alerts} open alert{s.open_alerts > 1 ? "s" : ""}</div>}
      {s.servers_total === 0 && <div className="loc">No servers yet</div>}
    </a>
  );
}

export function ServerDot({ v, now }: { v: Server; now: number }) {
  const state = v.retired_at ? "retired" : v.online ? "online" : `offline · last seen ${ago(v.last_seen_at, now)}`;
  return <span className={`dot ${v.online ? "ok" : ""} ${v.retired_at ? "retired" : ""}`} title={`${v.name}: ${state}`} />;
}

/**
 * The customer's latest digest. With `site`: only that Site's servers, from the data the digest was written from
 * (one line per server, named only when the Site has several); an older digest without that data shows its whole text.
 */
export function DigestCard({ org, site }: { org: Org; site?: Site }) {
  const [rows, setRows] = useState<Digest[]>([]);
  const [busy, setBusy] = useState(false);
  const load = useCallback(() => api.digests(org.id).then(setRows).catch(() => setRows([])), [org.id]);
  useEffect(() => { load(); }, [load]);
  const d = rows[0];
  const parts = site && d ? digestPartsFor(d.data, site) : null;
  return (
    <div className="card" style={{ marginTop: 12 }}>
      <div className="row"><h3 style={{ margin: 0 }}>Digest</h3><span className="muted small">{d ? `${d.day}${d.model ? ` · ${d.model}` : ""}${d.scoped ? " · your Sites only" : ""}` : "none yet"}</span><span className="spacer" />
        {(org.role === "admin" || org.role === "owner") && !d?.scoped && <button className="ghost small" disabled={busy} onClick={async () => { setBusy(true); try { await api.digestNow(org.id); await load(); } catch (e) { toast.error(e); } finally { setBusy(false); } }}>Generate now</button>}</div>
      {d && (!site || !parts) && <pre style={{ whiteSpace: "pre-wrap", margin: "8px 0 0", font: "inherit" }}>{d.text}</pre>}
      {d && parts && parts.length === 0 && <p className="muted small" style={{ marginBottom: 0 }}>This site had no servers when the digest was made.</p>}
      {d && parts && parts.length > 0 && (
        <>
          {parts.map((p) => <DigestPartView key={p.site_id} p={p} named={parts.length > 1} />)}
          <details className="small" style={{ marginTop: 8 }}><summary className="muted">Whole customer's digest</summary>
            <pre style={{ whiteSpace: "pre-wrap", margin: "6px 0 0", font: "inherit" }}>{d.text}</pre></details>
        </>
      )}
    </div>
  );
}

function DigestPartView({ p, named }: { p: DigestPart; named: boolean }) {
  return (
    <div className="digest-part">
      {named && <strong>{p.server_name ?? p.site_name}{p.online ? "" : " (offline)"} </strong>}
      <span>{p.headline ?? (p.online ? "No briefing yet." : "Offline when the digest was made.")}</span>
      {p.text && <p className="small" style={{ margin: "4px 0 0" }}>{p.text}</p>}
      {(p.cameras_down.length > 0 || p.open_alerts.length > 0) && (
        <div className="muted small">
          {p.cameras_down.length > 0 && `Cameras down: ${p.cameras_down.join(", ")}`}
          {p.cameras_down.length > 0 && p.open_alerts.length > 0 && " · "}
          {p.open_alerts.length > 0 && `${p.open_alerts.length} open alert${p.open_alerts.length > 1 ? "s" : ""} then`}
        </div>
      )}
    </div>
  );
}
