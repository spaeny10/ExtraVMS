/**
 * The Site's combined Live view: one grid over every camera of every server at the Site, with the activity of all of
 * them alongside. Each tile talks to its own server through the hub tunnel (siteApi(server) = /s/<server>/…), with
 * that server's TURN relay, so WHEP offers for one grid go to several /s/<id>/ prefixes.
 *
 * Streams share the page budget (MAX_LIVE, as on dashboards): tiles past it show a still with a Play button.
 * A server that is offline (or goes offline mid-view, seen on the Site's 15 s poll) shows its cameras from the hub's
 * registry as dark "Server offline" tiles: frames come through the tunnel, so there is no still to show.
 */
import { useEffect, useMemo, useReducer, useRef, useState } from "react";
import type { Camera as ServerCam } from "@site/api";
import { LiveTile } from "@site/Views";
import { LiveBudgetProvider, useBudget } from "@site/dashboard/Dashboard";
import { useIceServers } from "@site/dashboard/ice";
import { camKey, type DashboardSource } from "@site/dashboard/source";
import { EventsWidget } from "@site/dashboard/widgets/EventsWidget";
import type { Widget } from "@site/dashboard/types";
import { Icon, swipeHandlers, useIsPhone } from "@site/ui";
import { type Camera as RegistryCam, type Fleet, type Org, type Server, type Site, api } from "./api";
import { makeHubSource, siteApi } from "./hubSource";
import { EMPTY_LAYOUT, type LayoutAction, type LiveFocus, type LiveLayout, type Quality, applyFocus, arrange, gridCols, isVisible, layoutReducer, loadLayout, saveLayout, tileLabel,
  wrapIndex } from "./liveLayout";

/** live: full camera record from the server · loading: server online, cameras not fetched yet · unreachable: the
 *  server is online at the hub but its camera list failed (busy tunnel, restarting) · offline: the server is offline */
type TileState = "live" | "loading" | "unreachable" | "offline";
type Tile = { key: string; server: Server; id: string; name: string; state: TileState; cam: ServerCam | null; streamReady: boolean };

/** The hub source over just this Site's servers, so EventsWidget and the ICE cache work as on a dashboard. */
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

  if (servers.length === 0) return <p className="muted">This site has no servers yet.</p>;
  if (tiles.length === 0) return <p className="muted">{Object.keys(full).length || registry.length ? "No cameras at this site yet." : "Loading cameras…"}</p>;

  const tileProps = { source, multi, q, dispatch, hdUnsupported, onUnsupported: () => setHdUnsupported(true) };
  const events: Widget<"events"> = { id: `site-live-${site.id}`, type: "events", x: 0, y: 0, w: 1, h: 1, props: { sites: servers.map((s) => s.id), limit: 12 } };
  const activity = hideActivity ? null : <aside className="live-feed"><h3>Latest activity</h3><EventsWidget widget={events} source={source} /></aside>;
  if (isPhone) return <PhoneLive tiles={shown.length ? shown : ordered} activity={activity} {...tileProps} />;

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
        {activity}
      </div>
    </LiveBudgetProvider>
  );
}

type TileCommon = {
  source: DashboardSource; multi: boolean; q: (key: string) => Quality; dispatch: React.Dispatch<LayoutAction>;
  hdUnsupported: boolean; onUnsupported: () => void;
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

function SiteTile({ t, source, multi, q, dispatch, hdUnsupported, onUnsupported, extra, phone, onSwipe }: TileCommon & {
  t: Tile; extra?: React.ReactNode; phone?: boolean; onSwipe?: (dir: -1 | 1) => void;
}) {
  const box = useRef<HTMLDivElement>(null);
  const budget = useBudgetSlot(t.key, box);
  // TURN credentials are minted per server; until they arrive the tile shows a still rather than connecting twice
  const ice = useIceServers(source, t.server.id);
  const site = siteApi(t.server.id);
  const hd = q(t.key) === "hd";
  const playing = t.state === "live" && (phone || budget.playing(t.key)) && ice !== undefined;
  const [stillTs, setStillTs] = useState(() => Date.now() / 1000 - 3);
  useEffect(() => {
    if (playing || t.state !== "live") return;
    const i = setInterval(() => setStillTs(Date.now() / 1000 - 3), 10000);
    return () => clearInterval(i);
  }, [playing, t.state]);
  const label = tileLabel(t.server.name, t.name, multi);
  const qualityButtons = (
    <div className="segmented small-seg" title="Stream quality for this camera">
      <button className={!hd ? "active" : ""} onClick={() => dispatch({ type: "quality", key: t.key, quality: "sd" })}>SD</button>
      <button className={hd ? "active" : ""} disabled={hdUnsupported} onClick={() => dispatch({ type: "quality", key: t.key, quality: "hd" })}>HD</button>
    </div>
  );
  return (
    <div ref={box} className="site-live-cell">
      {playing && t.cam ? (
        <LiveTile c={t.cam} hd={hd} port={source.port} iceServers={ice} site={site} regionKey={t.key} phone={phone} onSwipe={onSwipe}
          onUnsupported={hd ? onUnsupported : undefined} bar={<>
            <span className={`dot ${t.streamReady ? "ok" : "bad"}`} title={t.streamReady ? "Recording" : "No stream"} />
            <span>{label}</span>
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
                {x.state === "live" ? <img src={`${siteApi(x.server.id).frameUrl(x.id, Date.now() / 1000 - 15, 320)}&r=${tick}`} alt="" /> : <div className="site-live-strip-off">offline</div>}
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
