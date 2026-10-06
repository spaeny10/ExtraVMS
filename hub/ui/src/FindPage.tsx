/**
 * Find / Ask across the customer's servers (/find). A Site's own Find tab is SiteFind (the server UI's Find over the
 * Site's servers); `site` here still scopes this page to one Site.
 * Results are server events shown with the server UI's EventCard (media fetched through that server's tunnel) and
 * labeled "Site · Server · Camera" (whereLabel). A click opens the event viewer in place (clip, synopsis, feedback), as
 * the server's own Find does; the viewer's "Open in Timeline" goes on to the Site's combined Timeline. Each card also
 * keeps a link to the same moment on the server's own console; footage moments (no event to show) open the Timeline.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import type { NvrEvent } from "@site/api";
import { EventCard } from "@site/Events";
import { toast } from "@site/ui";
import { type FleetSearch, type Org, type ServerTag, type Site, api, fmtTime } from "./api";
import { ACTIONS_PATH } from "./customer/fleetActions";
import { FleetAskResults, useFleetAsk } from "./fleetAskPanel";
import { type EventRef } from "./eventOpen";
import { HubEventDetail } from "./HubEventDetail";
import { useDirectVersion } from "./direct";
import { mediaApi } from "./hubSource";
import { whereLabel } from "./labels";
import { consoleTimelineHref, go } from "./nav";
import { siteTimelineHref } from "./timelineLink";

type Where = { site?: string | null; server?: string | null; camera?: string | null };

/**
 * What labels need beyond the search results: camera names (results carry ids only) and how many servers each Site
 * has (to drop the server from the label of a one-server Site). Inside a Site page that is the Site itself; across
 * the customer one /api/fleet read.
 */
export function useWhere(org: Org, site?: Site) {
  const [fleetSites, setFleetSites] = useState<Site[] | null>(null);
  useEffect(() => {
    if (site) return;
    api.fleet(org.id).then((f) => setFleetSites(f.orgs.find((o) => o.org.id === org.id)?.locations ?? [])).catch(() => setFleetSites([]));
  }, [org.id, site]);
  return useMemo(() => {
    const sites = site ? [site] : fleetSites ?? [];
    const count = new Map(sites.map((s) => [s.id, s.servers.filter((v) => !v.retired_at).length]));
    const cams = new Map<string, string>();
    for (const s of sites) for (const v of s.servers) for (const c of v.summary?.cameras ?? []) cams.set(`${v.id}/${c.id}`, c.name);
    const serverOf = (t: ServerTag) => t.server_id ?? t.site_id;
    const label = (t: ServerTag, camera?: string) => {
      const w: Where = { site: t.location_name, server: t.server_name ?? t.site_name, camera: camera ? cams.get(`${serverOf(t)}/${camera}`) ?? camera : null };
      return whereLabel(w, { showSite: !site, serverCount: t.location_id ? count.get(t.location_id) : undefined }) || (t.server_name ?? t.site_name);
    };
    /** just the camera's name, for the event viewer's title */
    const cameraName = (server: string) => (camera: string) => cams.get(`${server}/${camera}`) ?? camera;
    return { label, serverOf, cameraName };
  }, [site, fleetSites]);
}

export function FindPage({ org, site }: { org: Org; site?: Site }) {
  const [q, setQ] = useState(() => new URLSearchParams(location.search).get("q") ?? "");
  const fromUrl = useRef(!!new URLSearchParams(location.search).get("q"));
  const [res, setRes] = useState<FleetSearch | null>(null);
  const [busy, setBusy] = useState(false);
  const { label, serverOf, cameraName } = useWhere(org, site);
  const scope = site?.id;
  const fa = useFleetAsk(org, label, scope);
  const [open, setOpen] = useState<EventRef | null>(null);
  // snapshots come straight from a server this browser reaches on its LAN (re-rendered when that changes)
  useDirectVersion(useMemo(() => [...new Set((res?.events ?? []).map(serverOf))], [res, serverOf]));
  const search = async () => {
    if (!q.trim()) return;
    setBusy(true); fa.reset();
    try { setRes(await api.fleetSearch(org.id, q.trim(), undefined, scope)); } catch (e) { toast.error(e); } finally { setBusy(false); }
  };
  // arrived from a dashboard Ask box: run the question once, then drop it from the URL
  useEffect(() => {
    if (fromUrl.current) { fromUrl.current = false; history.replaceState(null, "", location.pathname); ask(); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const ask = async (anyway = false) => {
    if (!q.trim()) return;
    setRes(null);
    await fa.ask(q, anyway);
  };
  /** The event viewer in place; its "Open in Timeline" knows the Site (or falls back to the server's console). */
  const openEvent = (e: FleetSearch["events"][number]) => setOpen({ server: serverOf(e), id: e.id, location: e.location_id ?? (site && site.id) });
  return (
    <>
      {site ? <h3 style={{ marginTop: 0 }}>Find across {site.name}</h3> : <h2>Find across {org.name}</h2>}
      <form className="row" onSubmit={(e) => { e.preventDefault(); search(); }}>
        <input style={{ flex: 1, minWidth: 260 }} value={q} onChange={(e) => setQ(e.target.value)}
          placeholder={`Search ${site ? "this site" : "every server"}: "white pickup truck", "person at the back door last night"…`} />
        <button type="submit" disabled={busy || !q.trim()}>{busy ? "Searching…" : "Search"}</button>
        <button type="button" className="ghost" disabled={fa.asking || !q.trim()} onClick={() => ask()}
          title="Every server's assistant answers from its own footage. Instructions such as &quot;Migrate Ironsight to Hailo T1&quot; run from Customer › Actions">
          ✦ {site ? "Ask this site" : "Ask all servers"}
        </button>
      </form>
      <p className="muted small" style={{ margin: "4px 0 0" }}>Ask answers questions. To move cameras, quiet alerts or lock footage, use <a href={ACTIONS_PATH} onClick={go(ACTIONS_PATH)}>Customer › Actions</a>.</p>
      <FleetAskResults answers={fa.answers} instruction={fa.instruction} onAskAnyway={() => ask(true)} />
      {res && (
        <>
          <p className="muted small">{res.sites.map((s) => `${label(s)}: ${s.events} events, ${s.footage} moments${s.error ? ` (${s.error})` : ""}`).join(" · ")}{res.offline.length ? ` · offline: ${res.offline.join(", ")}` : ""}</p>
          {res.events.length === 0 && res.footage.length === 0 && <p className="muted">Nothing matched.</p>}
          <div className="event-grid">
            {res.events.map((e) => {
              const server = serverOf(e);
              return (
                <div key={`${server}-${e.id}`} className="find-hit">
                  <EventCard e={e as unknown as NvrEvent} cameraName={label(e, e.camera_id)} site={mediaApi(server)} onOpen={() => openEvent(e)} />
                  <a className="small muted find-hit-out" href={consoleTimelineHref(server, { cam: e.camera_id, event: e.id })}>Open on server ↗</a>
                </div>
              );
            })}
          </div>
          {res.footage.length > 0 && (
            <>
              <h3>Footage moments</h3>
              <div className="row">{res.footage.map((m, i) => {
                const server = serverOf(m);
                const text = `${label(m, m.camera_id)} · ${fmtTime(m.ts)}`;
                const out = consoleTimelineHref(server, { cam: m.camera_id, t: m.ts });
                return m.location_id ? (
                  <span key={i} className="find-moment">
                    <a className="chip" href={siteTimelineHref(m.location_id, server, m.camera_id, null, m.ts)} onClick={go(siteTimelineHref(m.location_id, server, m.camera_id, null, m.ts))}>{text}</a>
                    <a className="small muted" href={out} title="Open on server">↗</a>
                  </span>
                ) : <a key={i} className="chip" href={out}>{text}</a>;
              })}</div>
            </>
          )}
        </>
      )}
      {open && <HubEventDetail ev={open} cameraName={cameraName(open.server)} onClose={() => setOpen(null)} />}
    </>
  );
}
