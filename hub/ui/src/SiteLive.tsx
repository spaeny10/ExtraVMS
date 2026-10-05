/**
 * The Site's combined Live view: one grid over every camera of every server at the Site, with the activity of all of
 * them alongside. Each tile talks to its own server through the hub tunnel (siteApi(server) = /s/<server>/…), with
 * that server's TURN relay, so WHEP offers for one grid go to several /s/<id>/ prefixes. When this browser is on a
 * server's LAN (direct.ts), that server's video, stills and snapshots come straight from it (mediaApi); writes such as
 * PTZ and painted regions still go through the hub.
 *
 * Streams share the page budget (MAX_LIVE, as on dashboards): tiles past it show a still with a Play button.
 * A server that is offline (or goes offline mid-view, seen on the Site's 15 s poll) shows its cameras from the hub's
 * registry as dark "Server offline" tiles: frames come through the tunnel, so there is no still to show.
 *
 * "Latest activity" mirrors the server UI's LiveView: event cards that open the event viewer in place (HubEventDetail),
 * filtered by a region painted on a tile (only that camera's events whose path crossed it), and tiles with an event
 * in progress marked as alerting. The pool and the filtering are eventOpen.ts.
 */
import { useEffect, useMemo, useReducer, useRef, useState } from "react";
import type { Camera as ServerCam, NvrEvent } from "@site/api";
import { EventCard } from "@site/Events";
import { LiveTile } from "@site/Views";
import { LiveBudgetProvider, useBudget } from "@site/dashboard/Dashboard";
import { useIceServers } from "@site/dashboard/ice";
import { camKey, splitKey, type DashboardSource } from "@site/dashboard/source";
import type { FleetEvent } from "@site/dashboard/types";
import { regions, useRegions } from "@site/region";
import { Icon, swipeHandlers, useIsPhone } from "@site/ui";
import { type Camera as RegistryCam, type Fleet, type Org, type Server, type Site, api } from "./api";
import { type EventRef, applyLiveEvent, cameraNameFor, mergePool, regionFeed, removeLiveEvent, siteRegionKeys, tagServer } from "./eventOpen";
import { HubEventDetail } from "./HubEventDetail";
import { useDirect, useDirectVersion } from "./direct";
import { makeHubSource, mediaApi, siteApi } from "./hubSource";
import { EMPTY_LAYOUT, type LayoutAction, type LiveFocus, type LiveLayout, type Quality, applyFocus, arrange, gridCols, isVisible, layoutReducer, loadLayout, saveLayout, tileLabel,
  wrapIndex } from "./liveLayout";

/** live: full camera record from the server · loading: server online, cameras not fetched yet · unreachable: the
 *  server is online at the hub but its camera list failed (busy tunnel, restarting) · offline: the server is offline */
type TileState = "live" | "loading" | "unreachable" | "offline";
type Tile = { key: string; server: Server; id: string; name: string; state: TileState; cam: ServerCam | null; streamReady: boolean };

/** The hub source over just this Site's servers: the tiles' ICE cache and the fleet socket, as on a dashboard. */
function siteFleet(org: Org, site: Site): Fleet {
  // retired servers are left out: they would only add "Server · " prefixes to the activity feed
  return { orgs: [{ org, sites: site.servers.filter((s) => !s.retired_at), open_alerts: site.open_alerts }], now: Date.now() / 1000, offline_after_s: 0 };
}

/**
 * Embedding props (the SOC incident view; the Site's Live tab uses none of them):
 * `focus` puts the given tile keys (camKey) first or shows only them, and keeps them visible even if hidden in the
 * layout; `persist={false}` starts from the default layout and never writes this browser's stored one (an operator's
 * per-incident reshuffles shouldn't rearrange the customer's Live tab); `compact` drops the stream toolbar and the
 * camera chips; `hideActivity` drops the "Latest activity" column (the incident view has its own event list).
 */
export type SiteLiveProps = { org: Org; site: Site; focus?: LiveFocus | null; persist?: boolean; compact?: boolean; hideActivity?: boolean };

export function SiteLive({ org, site, focus: want = null, persist = true, compact = false, hideActivity = false }: SiteLiveProps) {
  const servers = useMemo(() => site.servers.filter((s) => !s.retired_at), [site.servers]);
  const multi = servers.length > 1;
  useDirectVersion(servers);  // re-render (feed snapshots, phone strip) when a server's media route changes
  // the Site object is replaced on every 15 s poll; a source rebuilt each time would make EventsWidget refetch and
  // re-open its socket. One source per Site, reading the latest servers through a ref, avoids that.
  const siteRef = useRef(site);
  siteRef.current = site;
  const source = useMemo<DashboardSource>(() => {
    const cur = () => makeHubSource(org, siteFleet(org, siteRef.current), []);
    return { ...cur(), cameras: () => cur().cameras(), sites: () => cur().sites() };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [org.id, site.id]);

  // full camera records (status, ptz, zones) from each online server; refetched when a server comes or goes,
  // and every minute for stream status
  const onlineKey = servers.filter((s) => s.online).map((s) => s.id).sort().join(",");
  const [full, setFull] = useState<Record<string, ServerCam[] | "error">>({});
  useEffect(() => {
    const ids = onlineKey ? onlineKey.split(",") : [];
    let alive = true;
    const pull = () => ids.forEach((id) => siteApi(id).cameras()
      .then((cams) => { if (alive) setFull((f) => ({ ...f, [id]: cams.filter((c) => c.enabled) })); })
      .catch(() => { if (alive) setFull((f) => ({ ...f, [id]: f[id] && f[id] !== "error" ? f[id] : "error" })); }));
    pull();
    const t = setInterval(pull, 60000);
    return () => { alive = false; clearInterval(t); };
  }, [onlineKey]);
  // the hub's registry: names for cameras of offline/unreachable servers (works without the tunnel)
  const [registry, setRegistry] = useState<RegistryCam[]>([]);
  useEffect(() => { api.locationCameras(site.id).then(setRegistry).catch(() => {}); }, [site.id, onlineKey]);

  const tiles = useMemo<Tile[]>(() => servers.flatMap((s): Tile[] => {
    const f = full[s.id];
    if (s.online && f && f !== "error") {
      return f.map((c) => ({ key: camKey(s.id, c.id), server: s, id: c.id, name: c.name, state: "live", cam: c, streamReady: !!c.status?.stream_ready }));
    }
    const state: TileState = !s.online ? "offline" : f === "error" ? "unreachable" : "loading";
    const reg = registry.filter((r) => r.server_id === s.id && r.enabled && !r.missing_since);
    // a server that just dropped may not be in the registry fetch yet: keep showing the cameras we already know
    const known = reg.length ? reg.map((r) => ({ id: r.camera_id, name: r.name })) : Array.isArray(f) ? f.map((c) => ({ id: c.id, name: c.name })) : [];
    return known.map((c) => ({ key: camKey(s.id, c.id), server: s, id: c.id, name: c.name, state, cam: null, streamReady: false }));
  }), [servers, full, registry]);

  const [layout, dispatch] = useReducer(layoutReducer, site.id, (id) => (persist ? loadLayout(id) : EMPTY_LAYOUT));
  useEffect(() => { if (persist) saveLayout(site.id, layout); }, [persist, site.id, layout]);
  const allKeys = tiles.map((t) => t.key);
  const byKey = new Map(tiles.map((t) => [t.key, t]));
  const ordered = applyFocus(arrange(allKeys, layout), want).map((k) => byKey.get(k)!);
  const pinned = new Set(want?.keys ?? []);
  const shown = ordered.filter((t) => pinned.has(t.key) || isVisible(layout, t.key));

  const [hdUnsupported, setHdUnsupported] = useState(false);
  const q = (key: string): Quality => (hdUnsupported ? "sd" : layout.quality[key] ?? "sd");
  const [expanded, setExpanded] = useState<string | null>(null);
  const isPhone = useIsPhone();
  // the SOC incident view hides the column (it has its own event list): no feed fetches or socket for it either
  const activity = useSiteActivity(site, servers, source, !hideActivity);
  const activeOf = (key: string) => activity.recent?.find((e) => camKey(e.site_id, e.camera_id) === key && (e.status === "open" || e.status === "pending"));

  if (servers.length === 0) return <p className="muted">This site has no servers yet.</p>;
  if (tiles.length === 0) return <p className="muted">{Object.keys(full).length || registry.length ? "No cameras at this site yet." : "Loading cameras…"}</p>;

  const tileProps = { source, multi, q, dispatch, hdUnsupported, onUnsupported: () => setHdUnsupported(true), activeOf };
  const feed = hideActivity ? null : <SiteActivity site={site} servers={servers} tiles={tiles} multi={multi} activity={activity} />;
  if (isPhone) return <PhoneLive tiles={shown.length ? shown : ordered} activity={feed} {...tileProps} />;

  const focused = expanded ? shown.find((t) => t.key === expanded) : undefined;
  const grid = focused ? [focused] : shown;
  const liveKeys = shown.filter((t) => t.state === "live").map((t) => t.key);
  const allHd = liveKeys.length > 0 && liveKeys.every((k) => q(k) === "hd");
  const allSd = liveKeys.every((k) => q(k) === "sd");
  return (
    <LiveBudgetProvider>
      <div className={`live-layout ${hideActivity ? "site-live-solo" : ""} ${compact ? "site-live-compact" : ""}`}>
        <div className="live-main">
          {!compact && <div className="toolbar live-toolbar">
            <span className="muted small">Stream</span>
            <div className="segmented">
              <button className={allSd ? "active" : ""} onClick={() => dispatch({ type: "allQuality", keys: liveKeys, quality: "sd" })} title="H.264 sub stream: low bandwidth, plays in any browser">All SD</button>
              <button className={allHd ? "active" : ""} disabled={hdUnsupported} onClick={() => dispatch({ type: "allQuality", keys: liveKeys, quality: "hd" })} title="Full-resolution H.265 main stream">All HD</button>
            </div>
            {hdUnsupported && <span className="muted small">This browser can't play the H.265 main stream, so live view is using SD.</span>}
          </div>}
          {!compact && <CameraChips tiles={ordered} layout={layout} multi={multi} dispatch={dispatch} />}
          <div className="live-grid" style={{ gridTemplateColumns: `repeat(${focused ? 1 : gridCols(grid.length)}, minmax(0, 1fr))` }}>
            {grid.map((t) => (
              <SiteTile key={t.key} t={t} {...tileProps}
                extra={<button className="ghost small" onClick={() => setExpanded(focused ? null : t.key)}>{focused ? "Grid" : "Expand"}</button>} />
            ))}
            {grid.length === 0 && <div className="empty">Every camera is hidden. Show some with the chips above, or Reset.</div>}
          </div>
        </div>
        {feed}
      </div>
    </LiveBudgetProvider>
  );
}

// ---------------------------------------------------------------- Latest activity (LiveView's feed, across servers)

const RECENT_LIMIT = 50; // the Site's recent events; a painted camera adds up to 100 of its own (as LiveView)
const SCOPED_LIMIT = 100;

type Activity = {
  /** the Site's recent events kept fresh from the fleet socket (null: loading) */
  recent: FleetEvent[] | null;
  /** recent + the painted cameras' deeper history */
  pool: FleetEvent[];
  /** painted lane keys of this Site's cameras */
  regionKeys: string[];
  regionMap: Record<string, Uint8Array>;
  offline: string[];
};

/**
 * LiveView's `recent + socket` pool and its painted-region fetches, for every server at the Site: the Site's latest
 * events from the hub, live updates from the fleet socket (this Site's servers only), and up to 100 recent events of
 * each painted camera from its own server.
 */
function useSiteActivity(site: Site, servers: Server[], source: DashboardSource, enabled: boolean): Activity {
  const serverKey = servers.map((s) => s.id).sort().join(",");
  const serversRef = useRef(servers);
  serversRef.current = servers;
  const [recent, setRecent] = useState<FleetEvent[] | null>(null);
  const [offline, setOffline] = useState<string[]>([]);
  useEffect(() => {
    if (!enabled) return;
    const own = new Set(serverKey.split(",").filter(Boolean));
    let alive = true;
    setRecent(null);
    // updates that arrive before the first answer are kept: merged over it, the live copy wins
    api.locationEvents(site.id, { limit: RECENT_LIMIT })
      .then((r) => { if (alive) { setRecent((prev) => mergePool(r.events, prev ?? [])); setOffline(r.offline); } })
      .catch(() => { if (alive) setRecent((prev) => prev ?? []); });
    const unsub = source.subscribe((m) => {
      if (!own.has(m.site_id)) return;
      if (m.type === "event_removed") setRecent((prev) => prev && removeLiveEvent(prev, m.site_id, m.id));
      else if (m.type === "event") setRecent((prev) => applyLiveEvent(prev ?? [], { ...m.event, site_id: m.site_id, site_name: m.site_name }));
    });
    return () => { alive = false; unsub(); };
  }, [enabled, site.id, serverKey, source]);

  const regionMap = useRegions();
  const regionKeys = useMemo(() => siteRegionKeys(regionMap, serverKey.split(",")), [regionMap, serverKey]);
  const scopeKey = regionKeys.join(",");
  const [scoped, setScoped] = useState<FleetEvent[]>([]);
  useEffect(() => {
    if (!enabled || !scopeKey) { setScoped([]); return; }
    let alive = true;
    const nameOf = (id: string) => serversRef.current.find((s) => s.id === id)?.name ?? id;
    Promise.all(scopeKey.split(",").map((k) => {
      const { server, id } = splitKey(k);
      return siteApi(server).events({ camera: id, status: "open,pending,verified", limit: SCOPED_LIMIT })
        .then((evs) => tagServer(evs, server, nameOf(server))).catch(() => [] as FleetEvent[]);
    })).then((lists) => { if (alive) setScoped(lists.flat()); });
    return () => { alive = false; };
  }, [enabled, scopeKey]);

  const pool = useMemo(() => (scopeKey ? mergePool(scoped, recent ?? []) : recent ?? []), [scopeKey, scoped, recent]);
  return { recent, pool, regionKeys, regionMap, offline };
}

/** The "Latest activity" column: LiveView's feed and RegionNote, with the event viewer opened in place. */
function SiteActivity({ site, servers, tiles, multi, activity: a }: { site: Site; servers: Server[]; tiles: Tile[]; multi: boolean; activity: Activity }) {
  const [open, setOpen] = useState<EventRef | null>(null);
  const names = useMemo(() => new Map(tiles.map((t) => [t.key, t.name])), [tiles]);
  /** "Server · Camera" on a multi-server Site, as the tiles are labelled */
  const label = (key: string) => {
    const t = tiles.find((x) => x.key === key);
    const { server, id } = splitKey(key);
    return t ? tileLabel(t.server.name, t.name, multi) : tileLabel(servers.find((s) => s.id === server)?.name ?? server, id, multi);
  };
  const feed = regionFeed(a.pool, a.regionMap, a.regionKeys);
  const offline = a.offline.map((id) => servers.find((s) => s.id === id)?.name ?? id);
  return (
    <>
      <aside className="live-feed">
        <h3>Latest activity</h3>
        {a.regionKeys.length > 0 && (
          <p className="muted small">
            Showing only {a.regionKeys.map(label).join(" and ")} events that passed through the painted region ·{" "}
            <button className="linkish" onClick={() => a.regionKeys.forEach((k) => regions.set(k, null))}>Clear</button>
          </p>
        )}
        {offline.length > 0 && <p className="muted small">Offline: {offline.join(", ")}</p>}
        {a.recent === null && <p className="muted">Loading…</p>}
        {a.recent !== null && a.pool.length === 0 && <p className="muted">Nothing yet.</p>}
        {a.pool.length > 0 && feed.length === 0 && <p className="muted">Nothing recent on that camera passed through the painted region.</p>}
        {feed.slice(0, 12).map((e) => (
          <EventCard key={`${e.site_id}-${e.id}`} e={e} cameraName={names.get(camKey(e.site_id, e.camera_id)) ?? e.camera_id}
            site={mediaApi(e.site_id)} siteName={multi ? e.site_name : undefined}
            onOpen={() => setOpen({ server: e.site_id, id: e.id, location: site.id })} />
        ))}
      </aside>
      {/* a sibling of the column, as in LiveView, so the column's scroll and card styles don't reach the viewer */}
      {open && <HubEventDetail ev={open} cameraName={cameraNameFor(names, open.server)} onClose={() => setOpen(null)} />}
    </>
  );
}

type TileCommon = {
  source: DashboardSource; multi: boolean; q: (key: string) => Quality; dispatch: React.Dispatch<LayoutAction>;
  hdUnsupported: boolean; onUnsupported: () => void;
  /** the camera's event in progress (open or pending), as LiveView marks a tile alerting */
  activeOf: (key: string) => NvrEvent | undefined;
};

/** Show/hide chips (eye toggles, like the Timeline's lanes), drag a chip onto another to reorder, Reset. */
function CameraChips({ tiles, layout, multi, dispatch }: { tiles: Tile[]; layout: LiveLayout; multi: boolean; dispatch: React.Dispatch<LayoutAction> }) {
  const all = tiles.map((t) => t.key);
  const [drag, setDrag] = useState<string | null>(null);
  const custom = layout.visible !== null || layout.order !== null || Object.keys(layout.quality).length > 0;
  return (
    <div className="row site-live-chips">
      {tiles.map((t) => {
        const on = isVisible(layout, t.key);
        return (
          <button key={t.key} className={`chip site-live-chip ${on ? "" : "hidden-cam"}`} draggable aria-pressed={on}
            title={`${on ? "Hide" : "Show"} ${t.name}${t.state === "offline" ? " (server offline)" : ""} · drag to reorder`}
            onClick={() => dispatch({ type: "toggle", key: t.key, all })}
            onDragStart={(e) => { setDrag(t.key); e.dataTransfer.effectAllowed = "move"; }}
            onDragOver={(e) => { if (drag && drag !== t.key) e.preventDefault(); }}
            onDrop={(e) => { e.preventDefault(); if (drag) dispatch({ type: "move", key: drag, before: t.key, all }); setDrag(null); }}
            onDragEnd={() => setDrag(null)}>
            <span aria-hidden="true">{on ? "👁" : "◌"}</span> {tileLabel(t.server.name, t.name, multi)}
          </button>
        );
      })}
      {custom && <button className="ghost small" onClick={() => dispatch({ type: "reset" })} title="Show every camera in server order, all SD">Reset</button>}
    </div>
  );
}

/** Registers the tile with the page's live budget while it is on screen (as CameraWidget does). */
function useBudgetSlot(key: string, box: React.RefObject<HTMLDivElement | null>) {
  const budget = useBudget();
  const { visible } = budget;
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    visible(key, true);
    const io = new IntersectionObserver(([e]) => visible(key, e.isIntersecting), { rootMargin: "100px" });
    io.observe(el);
    return () => { io.disconnect(); visible(key, false); };
  }, [visible, key, box]);
  return budget;
}

const STATE_TEXT: Record<Exclude<TileState, "live">, string> = { offline: "Server offline", unreachable: "Server not answering", loading: "Connecting…" };

function SiteTile({ t, source, multi, q, dispatch, hdUnsupported, onUnsupported, activeOf, extra, phone, onSwipe }: TileCommon & {
  t: Tile; extra?: React.ReactNode; phone?: boolean; onSwipe?: (dir: -1 | 1) => void;
}) {
  const box = useRef<HTMLDivElement>(null);
  const budget = useBudgetSlot(t.key, box);
  // TURN credentials are minted per server; until they arrive the tile shows a still rather than connecting twice
  const ice = useIceServers(source, t.server.id);
  useDirect(t.server);  // re-render when this server's media route changes (the tile reconnects on the new client)
  const site = mediaApi(t.server.id);
  const hd = q(t.key) === "hd";
  const playing = t.state === "live" && (phone || budget.playing(t.key)) && ice !== undefined;
  const [stillTs, setStillTs] = useState(() => Date.now() / 1000 - 3);
  useEffect(() => {
    if (playing || t.state !== "live") return;
    const i = setInterval(() => setStillTs(Date.now() / 1000 - 3), 10000);
    return () => clearInterval(i);
  }, [playing, t.state]);
  const label = tileLabel(t.server.name, t.name, multi);
  const active = activeOf(t.key);
  const qualityButtons = (
    <div className="segmented small-seg" title="Stream quality for this camera">
      <button className={!hd ? "active" : ""} onClick={() => dispatch({ type: "quality", key: t.key, quality: "sd" })}>SD</button>
      <button className={hd ? "active" : ""} disabled={hdUnsupported} onClick={() => dispatch({ type: "quality", key: t.key, quality: "hd" })}>HD</button>
    </div>
  );
  return (
    <div ref={box} className="site-live-cell">
      {playing && t.cam ? (
        <LiveTile c={t.cam} hd={hd} port={source.port} iceServers={ice} site={site} regionKey={t.key} phone={phone} onSwipe={onSwipe} active={active}
          onUnsupported={hd ? onUnsupported : undefined} bar={<>
            <span className={`dot ${t.streamReady ? "ok" : "bad"}`} title={t.streamReady ? "Recording" : "No stream"} />
            <span>{label}</span>
            {active && <span className={`label-chip ${active.camera_class}`}>{active.camera_class}</span>}
            <span className="spacer" />
            {qualityButtons}
            {extra}
          </>} />
      ) : (
        <div className={`tile ${t.state === "live" ? "" : "site-live-down"}`} {...(phone && onSwipe ? swipeHandlers(onSwipe) : {})}>
          <div className="player dash-still">
            {t.state === "live" ? <img src={site.frameUrl(t.id, stillTs, 640)} alt="" /> : <div className="player-state">{STATE_TEXT[t.state]}</div>}
            {t.state === "live" && (
              <button className="dash-play" onClick={() => budget.force(t.key)} title="Play live (another tile pauses if the page is at its limit)"><Icon name="play" size={22} /></button>
            )}
          </div>
          <div className="tile-bar">
            <span className={`dot ${t.state === "live" && t.streamReady ? "ok" : "bad"}`} />
            <span>{label}</span>
            {t.state === "live" && <span className="muted small">paused · still frame</span>}
            {t.state !== "live" && t.state !== "loading" && <span className="muted small">{t.state === "offline" ? "offline" : "unreachable"}</span>}
            <span className="spacer" />
            {t.state === "live" && qualityButtons}
            {extra}
          </div>
        </div>
      )}
    </div>
  );
}

/** Phone: one camera at a time, a strip of stills across every server's cameras, swipe or tap to switch (wraps). */
function PhoneLive({ tiles, activity, ...common }: TileCommon & { tiles: Tile[]; activity: React.ReactNode }) {
  const [idx, setIdx] = useState(0);
  const [tick, setTick] = useState(0);
  useEffect(() => { const t = setInterval(() => setTick((x) => x + 1), 10000); return () => clearInterval(t); }, []);
  const i = Math.min(idx, tiles.length - 1);
  const t = tiles[i];
  const step = (dir: -1 | 1) => setIdx(wrapIndex(i, dir, tiles.length));
  return (
    <LiveBudgetProvider>
      <div className="live-phone">
        <SiteTile key={t.key} t={t} {...common} phone onSwipe={step} />
        {tiles.length > 1 && (
          <div className="live-strip" role="tablist">
            {tiles.map((x, j) => (
              <button key={x.key} role="tab" aria-selected={j === i} className={`live-strip-item ${j === i ? "active" : ""}`} onClick={() => setIdx(j)}>
                {x.state === "live" ? <img src={`${mediaApi(x.server.id).frameUrl(x.id, Date.now() / 1000 - 15, 320)}&r=${tick}`} alt="" /> : <div className="site-live-strip-off">offline</div>}
                <span><span className={`dot ${x.state === "live" && x.streamReady ? "ok" : "bad"}`} /> {tileLabel(x.server.name, x.name, common.multi)}</span>
              </button>
            ))}
          </div>
        )}
        <p className="muted small center">Swipe the picture or tap a thumbnail to switch cameras.</p>
        {activity}
      </div>
    </LiveBudgetProvider>
  );
}
