/**
 * Customer → Servers: every server of the customer (retired ones too), grouped by Site. A toolbar searches and filters
 * (Site, "Has problems"); Table and Cards show the same fields: status and alerts, cameras (naming the ones down),
 * recording, AI, network (and, for admins, how long its live tiles take to start). Each server has one "Open console" link and its admin actions in a ⋯ menu (servers.tsx
 * ServerMenu). "+ Add server" opens the enroll form (ClaimBox). Retired servers sit in a collapsed section at the bottom.
 * The view and the filters are remembered per viewer (localStorage).
 */
import { useMemo, useState } from "react";
import type { LiveStats, Org, Server, Site } from "../api";
import { ago } from "../api";
import { CENTRAL_TAG } from "../central";
import { go, settingsHref, siteHref } from "../nav";
import { ServerMenu, useLiveStats } from "../servers";
import {
  type ServerFilters, NO_FILTERS, UNASSIGNED, aiText, cameraCounts, camDown, downText, filterServers, groupBySite, hasProblems, latestVersion,
  liveStartText, liveStartTitle, networkText, parseFilters, recordingText, updateAvailable,
} from "../serverList";
import { ClaimBox } from "./ClaimBox";

type View = "table" | "cards";
const VIEW_KEY = "customerServersView";
const FILTERS_KEY = "customerServersFilters";
const loadView = (): View => { try { return localStorage.getItem(VIEW_KEY) === "cards" ? "cards" : "table"; } catch { return "table"; } };
const loadFilters = (): ServerFilters => { try { return parseFilters(localStorage.getItem(FILTERS_KEY)); } catch { return NO_FILTERS; } };
const save = (key: string, v: string) => { try { localStorage.setItem(key, v); } catch { /* private mode */ } };

/** What a row and a card both need, worked out once per server. */
type Ctx = { now: number; latest: string | null; admin: boolean; sites: Site[]; onChanged: () => void; live: Record<string, LiveStats> };

/** Name, the "update available" badge, and its note · host (and the central recording tag, linking to its Site). */
function ServerName({ s, latest }: { s: Server; latest: string | null }) {
  const sub = [s.location, s.hostname].filter(Boolean).join(" · ");
  return (
    <>
      <strong title={s.id}>{s.name}</strong>
      {updateAvailable(s, latest) && <span className="chip small update-chip" title={`Runs ${s.version}; the newest among your servers is ${latest}`}>update available</span>}
      {s.retired_at ? <span className="alert-kind">retired</span> : null}
      {s.central && (s.location_id
        ? <div className="small"><a className="central-tag" href={settingsHref(s.location_id, "servers")} onClick={go(settingsHref(s.location_id, "servers"))}>{CENTRAL_TAG}</a></div>
        : <div className="small central-tag">{CENTRAL_TAG}</div>)}
      {sub && <div className="muted small">{sub}</div>}
    </>
  );
}

/** Online dot and "online" / "offline · 8 d ago", the open alerts as a badge linking to the Site's alerts. */
function Status({ s, now }: { s: Server; now: number }) {
  const alerts = s.location_id ? siteHref(s.location_id, "alerts") : "/alerts";
  const skew = s.clock_skew_s != null && Math.abs(s.clock_skew_s) > 30 ? `clock ${s.clock_skew_s > 0 ? "+" : ""}${Math.round(s.clock_skew_s)} s` : null;
  return (
    <>
      <span className="nowrap"><span className={`dot ${s.online ? "ok" : "bad"}`} /> {s.online ? "online" : `offline · ${ago(s.last_seen_at, now)}`}</span>
      {skew && <div className="small bad-text">{skew}</div>}
      {s.open_alerts > 0 && <div><a className="chip small warn alerts-chip" href={alerts} onClick={go(alerts)}>⚠ {s.open_alerts} alert{s.open_alerts > 1 ? "s" : ""}</a></div>}
    </>
  );
}

/** "6/7 up" and, when some are down, "1 down: Speaker Back Wall" (all of them in the tooltip). */
function Cameras({ s }: { s: Server }) {
  const c = cameraCounts(s);
  if (!c.total) return <span className="muted">—</span>;
  const down = downText(c.down);
  const title = (s.summary?.cameras ?? []).filter(camDown).map((x) => `${x.name}: ${(x.problems ?? []).join("; ") || "no stream"}`).join("\n");
  return (
    <>
      <span className="nowrap">{c.up}/{c.total} up</span>
      {s.summary?.site_link_down ? <div className="small bad-text">No camera reachable: the link to the site may be down</div>
        : down && <div className="small bad-text cams-down" title={title}>{down}</div>}
    </>
  );
}

const Network = ({ s }: { s: Server }) => { const t = networkText(s); return t ? <>{t}</> : <span className="muted">—</span>; };
/** "Live start p50 1.2 s · p95 3.4 s (42)" under Network in the table (nothing until there are enough views). */
function LiveStart({ st }: { st?: LiveStats }) {
  const t = liveStartText(st);
  return t && st ? <div className="muted small" title={liveStartTitle(st)}>Live start {t}</div> : null;
}

function Row({ s, ctx }: { s: Server; ctx: Ctx }) {
  return (
    <tr className={s.online || s.retired_at ? "" : "offline-row"}>
      <td className="lead"><ServerName s={s} latest={ctx.latest} /></td>
      <td><Status s={s} now={ctx.now} /></td>
      <td data-label="Cameras"><Cameras s={s} /></td>
      <td className="small" data-label="Recording" title={s.central ? (s.central.mode === "vpn" ? "Central recording over SpeedFusion" : "Central recording over port forwards") : "Free disk, and how long it lasts at the cameras' current bitrate"}>{recordingText(s)}</td>
      <td className="small" data-label="AI">{aiText(s)}</td>
      <td className="small" data-label="Network"><Network s={s} /><LiveStart st={ctx.live[s.id]} /></td>
      <td className="acts"><ServerMenu s={s} admin={ctx.admin} sites={ctx.sites} onChanged={ctx.onChanged} /></td>
    </tr>
  );
}

/** The Cards view: the table's fields in the same order, and every camera as a chip behind "Show all cameras". */
function Card({ s, ctx }: { s: Server; ctx: Ctx }) {
  const [all, setAll] = useState(false);
  const cams = s.summary?.cameras ?? [];
  const live = liveStartText(ctx.live[s.id]);
  return (
    <div className={`site-card server-list-card ${s.online ? "" : "offline"} ${s.retired_at ? "retired" : ""}`}>
      <div className="head"><div className="grow"><ServerName s={s} latest={ctx.latest} /></div></div>
      <div className="stats kv">
        <span>Status</span><div><Status s={s} now={ctx.now} /></div>
        <span>Cameras</span><div><Cameras s={s} /></div>
        <span>Recording</span><div>{recordingText(s)}</div>
        <span>AI</span><div>{aiText(s)}</div>
        <span>Network</span><div><Network s={s} /></div>
        {live && <><span>Live start</span><div title={liveStartTitle(ctx.live[s.id])}>{live}</div></>}
      </div>
      {cams.length > 0 && <button className="linkish" aria-expanded={all} onClick={() => setAll(!all)}>{all ? "Hide cameras" : "Show all cameras"}</button>}
      {all && (
        <div className="cams">{cams.map((c) => <span key={c.id} className={`cam ${camDown(c) ? "bad" : ""}`} title={(c.problems ?? []).join("; ") || (c.stream_ready ? "streaming" : "no stream")}>
          {c.name}{c.ptz && !c.ptz.at_home ? " ↗" : ""}</span>)}</div>
      )}
      <div className="row server-actions"><ServerMenu s={s} admin={ctx.admin} sites={ctx.sites} onChanged={ctx.onChanged} /></div>
    </div>
  );
}

/** A group's heading: the Site (linking to it) and how many servers, or "Unassigned". */
function GroupHead({ id, name, count }: { id?: string; name: string; count: number }) {
  const n = `${count} server${count === 1 ? "" : "s"}`;
  return id
    ? <><a href={settingsHref(id, "servers")} onClick={go(settingsHref(id, "servers"))}>{name}</a> <span className="muted small">· {n}</span></>
    : <>{name} <span className="muted small">· {n}</span></>;
}

const COLS = 7;

export function ServersBox({ org, servers, sites, admin, onChanged }: { org: Org; servers: Server[]; sites: Site[]; admin: boolean; onChanged: () => void }) {
  const [view, setViewState] = useState<View>(loadView);
  const [filters, setFiltersState] = useState<ServerFilters>(loadFilters);
  const [adding, setAdding] = useState(false);
  const [showRetired, setShowRetired] = useState(false);
  const setView = (v: View) => { setViewState(v); save(VIEW_KEY, v); };
  const setFilters = (f: Partial<ServerFilters>) => setFiltersState((old) => { const n = { ...old, ...f }; save(FILTERS_KEY, JSON.stringify(n)); return n; });
  const now = Date.now() / 1000;
  const siteNames = useMemo(() => Object.fromEntries(sites.map((l) => [l.id, l.name])), [sites]);
  // a remembered Site that is gone (or not loaded yet) filters nothing out
  const site = filters.site === UNASSIGNED || siteNames[filters.site] ? filters.site : "";
  const shown = useMemo(() => filterServers(servers, { ...filters, site }), [servers, filters, site]);
  const { groups, unassigned, retired } = useMemo(() => groupBySite(shown, siteNames), [shown, siteNames]);
  const live = useLiveStats(servers, admin);
  const ctx: Ctx = { now, latest: latestVersion(servers), admin, sites, onChanged, live };
  const problems = servers.filter(hasProblems).length;
  const filtered = !!(filters.q.trim() || site || filters.problems);
  const sections = [
    ...groups.map((g) => ({ key: g.id, head: <GroupHead id={g.id} name={g.name} count={g.servers.length} />, servers: g.servers })),
    ...(unassigned.length ? [{ key: UNASSIGNED, head: <GroupHead name="Unassigned" count={unassigned.length} />, servers: unassigned }] : []),
  ];
  const retiredHead = (
    <button className="linkish retired-toggle" aria-expanded={showRetired} onClick={() => setShowRetired(!showRetired)}>{showRetired ? "▾" : "▸"} Retired ({retired.length})</button>
  );
  return (
    <div className="card servers-box">
      <div className="row">
        <h3 style={{ margin: 0 }}>Servers <span className="muted small">the NVR boxes enrolled with this hub</span></h3>
        <span className="spacer" />
        {admin && <button className={adding ? "ghost small" : "small"} aria-expanded={adding} onClick={() => setAdding(!adding)}>{adding ? "Close" : "+ Add server"}</button>}
      </div>
      {adding && <ClaimBox org={org} sites={sites} onDone={onChanged} initialSite={site && site !== UNASSIGNED ? site : undefined} />}
      <div className="row servers-toolbar">
        <input type="search" className="servers-search" value={filters.q} placeholder="Search servers, notes, hosts, sites, cameras" aria-label="Search servers"
          onChange={(e) => setFilters({ q: e.target.value })} />
        <select value={site} aria-label="Site" onChange={(e) => setFilters({ site: e.target.value })}>
          <option value="">All sites</option>
          {[...sites].sort((a, b) => a.name.localeCompare(b.name)).map((l) => <option key={l.id} value={l.id}>{l.name}</option>)}
          {servers.some((s) => !s.location_id) && <option value={UNASSIGNED}>Unassigned</option>}
        </select>
        <button className={`chip ${filters.problems ? "on" : ""}`} aria-pressed={filters.problems} title="Offline, a camera down, or open alerts"
          onClick={() => setFilters({ problems: !filters.problems })}>Has problems{problems ? ` (${problems})` : ""}</button>
        <span className="spacer" />
        <div className="segmented small-seg">
          <button className={view === "table" ? "active" : ""} onClick={() => setView("table")}>Table</button>
          <button className={view === "cards" ? "active" : ""} onClick={() => setView("cards")}>Cards</button>
        </div>
      </div>
      {servers.length === 0 ? <p className="muted">None yet.{admin ? " Use + Add server with the claim code the server shows." : ""}</p>
        : shown.length === 0 ? <p className="muted">No server matches. {filtered && <button className="linkish" onClick={() => setFilters(NO_FILTERS)}>Clear the filters</button>}</p>
        : view === "cards" ? (
          <>
            {sections.map((g) => (
              <section key={g.key} className="server-group">
                <h4 className="server-group-head">{g.head}</h4>
                <div className="site-grid">{g.servers.map((s) => <Card key={s.id} s={s} ctx={ctx} />)}</div>
              </section>))}
            {retired.length > 0 && (
              <section className="server-group">
                <h4 className="server-group-head">{retiredHead}</h4>
                {showRetired && <div className="site-grid">{retired.map((s) => <Card key={s.id} s={s} ctx={ctx} />)}</div>}
              </section>)}
          </>
        ) : (
          <table className="hub-table stack server-table">
            <thead><tr><th>Server</th><th>Status</th><th>Cameras</th><th>Recording</th><th>AI</th><th>Network</th><th /></tr></thead>
            {sections.map((g) => (
              <tbody key={g.key}>
                <tr className="group-row"><th colSpan={COLS}>{g.head}</th></tr>
                {g.servers.map((s) => <Row key={s.id} s={s} ctx={ctx} />)}
              </tbody>))}
            {retired.length > 0 && (
              <tbody className="retired-rows">
                <tr className="group-row"><th colSpan={COLS}>{retiredHead}</th></tr>
                {showRetired && retired.map((s) => <Row key={s.id} s={s} ctx={ctx} />)}
              </tbody>)}
          </table>
        )}
    </div>
  );
}
