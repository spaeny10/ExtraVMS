/**
 * Find / Ask across the customer's servers, or (with `site`) across one Site's servers: the Site page's Find tab.
 * Results are server events shown with the server UI's EventCard (media fetched through that server's tunnel) and
 * labelled "Site · Server · Camera" (whereLabel). A click opens the moment in the Site's combined Timeline; each card
 * also keeps a link to the same moment on the server's own console.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import type { NvrEvent } from "@site/api";
import { EventCard } from "@site/Events";
import { toast } from "@site/ui";
import { type ExecPlan, type FleetSearch, type Org, type ServerTag, type Site, api, fleetAsk, fmtTime } from "./api";
import { FleetActionCard, planAction } from "./customer/FleetActionsPage";
import { siteApi } from "./hubSource";
import { whereLabel } from "./labels";
import { consoleHref, consoleTimelineHref, go, navigate } from "./nav";
import { siteTimelineHref } from "./timelineLink";

type Answer = { name: string; text: string; error?: string; done?: boolean };
type Where = { site?: string | null; server?: string | null; camera?: string | null };

/**
 * What labels need beyond the search results: camera names (results carry ids only) and how many servers each Site
 * has (to drop the server from the label of a one-server Site). Inside a Site page that is the Site itself; across
 * the customer one /api/fleet read.
 */
function useWhere(org: Org, site?: Site) {
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
    return { label, serverOf };
  }, [site, fleetSites]);
}

export function FindPage({ org, site }: { org: Org; site?: Site }) {
  const [q, setQ] = useState(() => new URLSearchParams(location.search).get("q") ?? "");
  const fromUrl = useRef(!!new URLSearchParams(location.search).get("q"));
  const [res, setRes] = useState<FleetSearch | null>(null);
  const [busy, setBusy] = useState(false);
  const [answers, setAnswers] = useState<Record<string, Answer>>({});
  const [asking, setAsking] = useState(false);
  const [action, setAction] = useState<ExecPlan | null>(null);
  const { label, serverOf } = useWhere(org, site);
  const scope = site?.id;
  const search = async () => {
    if (!q.trim()) return;
    setBusy(true); setAnswers({}); setAction(null);
    try { setRes(await api.fleetSearch(org.id, q.trim(), undefined, scope)); } catch (e) { toast.error(e); } finally { setBusy(false); }
  };
  // arrived from a dashboard Ask box: run the question once, then drop it from the URL
  useEffect(() => {
    if (fromUrl.current) { fromUrl.current = false; history.replaceState(null, "", location.pathname); ask(); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const ask = async () => {
    if (!q.trim()) return;
    setAsking(true); setRes(null); setAnswers({}); setAction(null);
    // an instruction ("Migrate Ironsight to Hailo T1") gets a confirmation card instead of going to the servers;
    // if the planner fails the question is simply asked as before
    const plan = await planAction(org, q.trim());
    if (plan) { setAction(plan); setAsking(false); return; }
    try {
      await fleetAsk(org.id, q.trim(), (c) => {
        const tag = c as unknown as ServerTag & { site?: string };
        const id = c.site as string | undefined;
        if (c.type === "sites") {
          const init: Record<string, Answer> = {};
          for (const s of c.sites as (ServerTag & { site: string })[]) init[s.site] = { name: label({ ...s, site_id: s.site }), text: "" };
          setAnswers(init);
          return;
        }
        if (!id) return;
        setAnswers((a) => {
          const cur = a[id] ?? { name: label({ ...tag, site_id: id, site_name: String(c.site_name ?? id) }), text: "" };
          if (c.type === "delta") return { ...a, [id]: { ...cur, text: cur.text + String(c.text ?? "") } };
          if (c.type === "error") return { ...a, [id]: { ...cur, error: String(c.error), done: true } };
          if (c.type === "site_done" || c.type === "done") return { ...a, [id]: { ...cur, done: true } };
          return a;
        });
      }, scope);
    } catch (e) { toast.error(e); } finally { setAsking(false); }
  };
  /** In-app to the Site's combined Timeline; a server in no Site has only its console. */
  const openEvent = (e: FleetSearch["events"][number]) => {
    const server = serverOf(e);
    if (e.location_id) navigate(siteTimelineHref(e.location_id, server, e.camera_id, e.id));
    else location.href = consoleTimelineHref(server, { cam: e.camera_id, event: e.id });
  };
  return (
    <>
      {site ? <h3 style={{ marginTop: 0 }}>Find across {site.name}</h3> : <h2>Find across {org.name}</h2>}
      <form className="row" onSubmit={(e) => { e.preventDefault(); search(); }}>
        <input style={{ flex: 1, minWidth: 260 }} value={q} onChange={(e) => setQ(e.target.value)}
          placeholder={`Search ${site ? "this site" : "every server"}: "white pickup truck", "person at the back door last night"…`} />
        <button type="submit" disabled={busy || !q.trim()}>{busy ? "Searching…" : "Search"}</button>
        <button type="button" className="ghost" disabled={asking || !q.trim()} onClick={ask}
          title="Every server's assistant answers from its own footage. Instructions such as &quot;Migrate Ironsight to Hailo T1&quot; show a confirmation card instead">
          ✦ {site ? "Ask this site" : "Ask all servers"}
        </button>
      </form>
      <p className="small" style={{ margin: "4px 0 0" }}><a href="/customer/actions" onClick={go("/customer/actions")}>What can I ask the hub to do?</a></p>
      {action && <FleetActionCard org={org} plan={action} onClose={() => setAction(null)} />}
      {res && (
        <>
          <p className="muted small">{res.sites.map((s) => `${label(s)}: ${s.events} events, ${s.footage} moments${s.error ? ` (${s.error})` : ""}`).join(" · ")}{res.offline.length ? ` · offline: ${res.offline.join(", ")}` : ""}</p>
          {res.events.length === 0 && res.footage.length === 0 && <p className="muted">Nothing matched.</p>}
          <div className="event-grid">
            {res.events.map((e) => {
              const server = serverOf(e);
              return (
                <div key={`${server}-${e.id}`} className="find-hit">
                  <EventCard e={e as unknown as NvrEvent} cameraName={label(e, e.camera_id)} site={siteApi(server)} onOpen={() => openEvent(e)} />
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
      {Object.keys(answers).length > 0 && (
        <div className="site-grid" style={{ marginTop: 12 }}>
          {Object.entries(answers).map(([id, a]) => (
            <div key={id} className="site-card">
              <div className="head"><strong>{a.name}</strong><span className="spacer" /><span className="muted small">{a.done ? "" : "thinking…"}</span></div>
              {a.error ? <p className="small" style={{ color: "var(--bad)" }}>{a.error}</p> : <pre style={{ whiteSpace: "pre-wrap", font: "inherit", margin: "6px 0 0" }}>{a.text || (a.done ? "No answer." : "")}</pre>}
              <a className="small" href={consoleHref(id, "find")}>Open this server's Find →</a>
            </div>
          ))}
        </div>
      )}
    </>
  );
}
