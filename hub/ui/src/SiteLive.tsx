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
 * in progress marked as alerting. The pool and the filtering are eventOpen.ts. The Activity button hides the column
 * (the grid takes the full width).
 *
 * Layouts, as on the Timeline: "All cameras" or a named layout saved on the hub (per user and Site, spanning its
 * servers: hub/hub/layouts.py), with Save / Save as new / Rename / Delete and a dot while the grid differs from it. A
 * layout holds which cameras show and in what order, their streams, the grid's columns and the activity column. Tiles
 * reorder by dragging the ⠿ grip in their bar (desktop); the phone picks a layout only. The logic is liveLayout.ts.
 */
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import type { Camera as ServerCam, NvrEvent } from "@site/api";
import { EventCard } from "@site/Events";
import { FeedMore, LiveTile, RegionChips, useFeedCount } from "@site/Views";
import { LiveBudgetProvider, useBudget } from "@site/dashboard/Dashboard";
import { useIceServers } from "@site/dashboard/ice";
import { camKey, splitKey, type DashboardSource } from "@site/dashboard/source";
import type { FleetEvent } from "@site/dashboard/types";
import { regionFetchKey, useRegionResults, useRegions } from "@site/region";
import { Icon, confirmDialog, promptDialog, swipeHandlers, toast, useIsPhone } from "@site/ui";
import { type Camera as RegistryCam, type Fleet, type Org, type Server, type Site, type SiteLayout, api } from "./api";
import { type EventRef, applyLiveEvent, cameraNameFor, mergePool, regionFeed, removeLiveEvent, siteRegionKeys, tagServer } from "./eventOpen";
import { HubEventDetail } from "./HubEventDetail";
import { useDirect, useDirectVersion } from "./direct";
import { makeHubSource, mediaApi, siteApi } from "./hubSource";
import { COLUMN_CHOICES, EMPTY_LAYOUT, type LayoutAction, type LiveFocus, type LiveLayout, type Quality, applyFocus, arrange, coerceLayout, isDirty, isVisible,
  layoutCols, layoutReducer, loadActivityPref, loadLayout, loadLayoutId, saveActivityPref, saveLayout, saveLayoutId, tileLabel, toSavedConfig,
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
 * per-incident reshuffles shouldn't rearrange the customer's Live tab); `compact` drops the stream toolbar (and with it the
 * Cameras picker and the layout picker); `hideActivity` drops the "Latest activity" column and its button (the
 * incident view has its own event list).
 */
export type SiteLiveProps = { org: Org; site: Site; focus?: LiveFocus | null; persist?: boolean; compact?: boolean; hideActivity?: boolean };

export function SiteLive(props: SiteLiveProps) {
  // keyed by Site: the working arrangement and the picked layout are per Site, never carried over to the next one
  return <SiteLiveView key={props.site.id} {...props} />;
}

function SiteLiveView({ org, site, focus: want = null, persist = true, compact = false, hideActivity = false }: SiteLiveProps) {
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

  // the working arrangement (this browser, per Site); under "All cameras" the activity column follows the viewer's
  // standing preference, under a named layout the layout's own setting
  const [layout, dispatch] = useReducer(layoutReducer, site.id, (id) => {
    if (!persist) return EMPTY_LAYOUT;
    const l = loadLayout(id);
    return loadLayoutId(id) ? l : { ...l, activity: loadActivityPref() };
  });
  useEffect(() => { if (persist) saveLayout(site.id, layout); }, [persist, site.id, layout]);
  // named layouts belong to the Site's own Live tab; an embed that doesn't persist (the SOC) has none
  const named = useNamedLayouts(site.id, persist && !compact);
  const allKeys = tiles.map((t) => t.key);
  const byKey = new Map(tiles.map((t) => [t.key, t]));
  const ordered = applyFocus(arrange(allKeys, layout), want).map((k) => byKey.get(k)!);
  const pinned = new Set(want?.keys ?? []);
  const shown = ordered.filter((t) => pinned.has(t.key) || isVisible(layout, t.key));
  const base = named.layoutId ? named.picked?.config : EMPTY_LAYOUT;   // undefined: the picked layout isn't loaded yet
  const dirty = !!base && isDirty(allKeys, layout, base, named.layoutId !== 0);
  const pickLayout = (id: number) => {
    const l = named.list?.find((x) => x.id === id);
    dispatch({ type: "load", layout: l ? l.config : { ...EMPTY_LAYOUT, activity: loadActivityPref() } });
    named.setLayoutId(l ? l.id : 0);
  };
  const revert = () => pickLayout(named.layoutId);
  const setActivity = (on: boolean) => {
    dispatch({ type: "activity", on });
    if (persist && !named.layoutId) saveActivityPref(on);
  };
  const drag = useTileDrag((key, onto) => dispatch({ type: "drop", key, onto, all: allKeys }));

  const [hdUnsupported, setHdUnsupported] = useState(false);
  const q = (key: string): Quality => (hdUnsupported ? "sd" : layout.quality[key] ?? "sd");
  const [expanded, setExpanded] = useState<string | null>(null);
  const isPhone = useIsPhone();
  // the column shows unless the embed drops it (the SOC) or the viewer hid it (a phone has it under the picture: always)
  const showFeed = !hideActivity && (isPhone || layout.activity);
  // the SOC incident view hides the column (it has its own event list): no feed fetches or socket for it either. Hidden
  // by the viewer, the Site's recent events still mark alerting tiles; only the painted regions' fetches stop.
  const activity = useSiteActivity(site, servers, source, !hideActivity, showFeed);
  const activeOf = (key: string) => activity.recent?.find((e) => camKey(e.site_id, e.camera_id) === key && (e.status === "open" || e.status === "pending"));

  if (servers.length === 0) return <p className="muted">This site has no servers yet.</p>;
  if (tiles.length === 0) return <p className="muted">{Object.keys(full).length || registry.length ? "No cameras at this site yet." : "Loading cameras…"}</p>;

  const tileProps = { source, multi, q, dispatch, hdUnsupported, onUnsupported: () => setHdUnsupported(true), activeOf };
  const feed = showFeed ? <SiteActivity site={site} servers={servers} tiles={tiles} multi={multi} activity={activity} /> : null;
  if (isPhone) {
    // the phone shows one camera at a time: a layout only sets which cameras are in the strip and their order
    const picker = named.enabled && !!named.list?.length && (
      <div className="row site-live-phone-pick">
        <span className="muted small">Layout</span>
        <LayoutSelect named={named} onPick={pickLayout} />
        {dirty && <span className="dirty-dot" title="Changed since this layout was picked">●</span>}
      </div>
    );
    return <PhoneLive tiles={shown.length ? shown : ordered} activity={feed} picker={picker || null} {...tileProps} />;
  }

  const focused = expanded ? shown.find((t) => t.key === expanded) : undefined;
  const grid = focused ? [focused] : shown;
  const draggable = !compact && grid.length > 1;
  const liveKeys = shown.filter((t) => t.state === "live").map((t) => t.key);
  const allHd = liveKeys.length > 0 && liveKeys.every((k) => q(k) === "hd");
  const allSd = liveKeys.every((k) => q(k) === "sd");
  return (
    <LiveBudgetProvider>
      <div className={`live-layout ${showFeed ? "" : "site-live-solo"} ${compact ? "site-live-compact" : ""}`}>
        <div className="live-main">
          {/* LiveView's one slim toolbar; which cameras show (and their order) sits behind "Cameras" in the same row
              rather than a row of chips of its own, so the grid starts as high as on the server */}
          {!compact && <div className="toolbar live-toolbar">
            {named.enabled && <LayoutPicker named={named} dirty={dirty} onPick={pickLayout} onRevert={revert} current={() => toSavedConfig(allKeys, layout)} />}
            <span className="muted small">Stream</span>
            <div className="segmented">
              <button className={allSd ? "active" : ""} onClick={() => dispatch({ type: "allQuality", keys: liveKeys, quality: "sd" })} title="H.264 sub stream: low bandwidth, plays in any browser">All SD</button>
              <button className={allHd ? "active" : ""} disabled={hdUnsupported} onClick={() => dispatch({ type: "allQuality", keys: liveKeys, quality: "hd" })} title="Full-resolution H.265 main stream">All HD</button>
            </div>
            {hdUnsupported && <span className="muted small">This browser can't play the H.265 main stream, so live view is using SD.</span>}
            <span className="spacer" />
            <select className="site-live-cols" value={layout.cols ?? 0} title="Grid columns" aria-label="Grid columns"
              onChange={(e) => dispatch({ type: "cols", cols: Number(e.target.value) || null })}>
              <option value={0}>Auto columns</option>
              {COLUMN_CHOICES.map((n) => <option key={n} value={n}>{n} column{n > 1 ? "s" : ""}</option>)}
            </select>
            {!hideActivity && (
              <button className={`ghost small ${layout.activity ? "on" : ""}`} aria-pressed={layout.activity} onClick={() => setActivity(!layout.activity)}
                title={layout.activity ? "Hide the Latest activity column: the cameras take the full width" : "Show the Latest activity column"}>
                Activity
              </button>
            )}
            <CameraPicker tiles={ordered} layout={layout} multi={multi} dispatch={dispatch} dirty={dirty} onRevert={revert}
              revertLabel={named.layoutId ? "Revert to the saved layout" : "Reset layout"} />
          </div>}
          <div className="live-grid" style={{ gridTemplateColumns: `repeat(${focused ? 1 : layoutCols(layout, grid.length)}, minmax(0, 1fr))` }}>
            {grid.map((t) => (
              <SiteTile key={t.key} t={t} {...tileProps}
                drag={draggable ? { onGrip: (e) => drag.start(t.key, tileLabel(t.server.name, t.name, multi), e), dragging: drag.dragKey === t.key,
                  dropTarget: drag.dropKey === t.key } : undefined}
                extra={<button className="ghost small" onClick={() => setExpanded(focused ? null : t.key)}>{focused ? "Grid" : "Expand"}</button>} />
            ))}
            {grid.length === 0 && <div className="empty">Every camera is hidden. Show some under Cameras above, or Reset.</div>}
          </div>
          {drag.ghost && <div className="drag-ghost" style={{ left: drag.ghost.x, top: drag.ghost.y }}>{drag.ghost.name}</div>}
        </div>
        {feed}
      </div>
    </LiveBudgetProvider>
  );
}

// ---------------------------------------------------------------- named layouts (saved on the hub) and tile drag

type NamedLayout = Omit<SiteLayout, "config"> & { config: LiveLayout };
type NamedLayouts = ReturnType<typeof useNamedLayouts>;

/**
 * The signed-in user's named Live layouts of this Site (the hub's /api/locations/{id}/layouts) and which one is
 * picked (this browser, per Site; 0 = "All cameras"). `list` is null until the hub answers (or when it can't).
 */
function useNamedLayouts(siteId: string, enabled: boolean) {
  const [list, setList] = useState<NamedLayout[] | null>(null);
  const [layoutId, setLayoutIdState] = useState(() => (enabled ? loadLayoutId(siteId) : 0));
  const setLayoutId = (id: number) => {
    setLayoutIdState(id);
    if (enabled) saveLayoutId(siteId, id);
  };
  const reload = useCallback(() => api.siteLayouts(siteId)
    .then((ls) => setList(ls.map((l) => ({ ...l, config: coerceLayout(l.config) }))))
    .catch(() => {}), [siteId]);   // a hub that can't answer keeps what was listed: the picked layout stays picked
  useEffect(() => { if (enabled) reload(); }, [enabled, reload]);
  // the picked layout is gone (deleted in another browser): "All cameras", keeping what is on screen (it shows changed)
  useEffect(() => {
    if (list && layoutId && !list.some((l) => l.id === layoutId)) {
      setLayoutIdState(0);
      if (enabled) saveLayoutId(siteId, 0);
    }
  }, [list, layoutId, enabled, siteId]);
  return { siteId, enabled, list, layoutId, setLayoutId, reload, picked: list?.find((l) => l.id === layoutId) };
}

function LayoutSelect({ named, onPick }: { named: NamedLayouts; onPick: (id: number) => void }) {
  const { list, layoutId } = named;
  return (
    <select className="site-live-layout-pick" value={layoutId} onChange={(e) => onPick(Number(e.target.value))} aria-label="Layout">
      <option value={0}>All cameras</option>
      {/* picked in an earlier visit, the list not loaded (yet) */}
      {layoutId !== 0 && !list?.some((l) => l.id === layoutId) && <option value={layoutId}>Saved layout…</option>}
      {list?.map((l) => <option key={l.id} value={l.id}>{l.name}</option>)}
    </select>
  );
}

/**
 * The Timeline's "Layout" row for Live: the picker, a dot while the grid differs from the picked layout, Save (or
 * "Save as…" under All cameras), and a ⋯ menu with Save as new, Rename, Delete and Revert.
 */
function LayoutPicker({ named, dirty, onPick, onRevert, current }: {
  named: NamedLayouts; dirty: boolean; onPick: (id: number) => void; onRevert: () => void;
  /** the arrangement on screen as a layout is saved (liveLayout.toSavedConfig) */
  current: () => LiveLayout;
}) {
  const [open, setOpen] = useState(false);
  const { picked } = named;
  const failed = (e: unknown) => toast.error(String(e).includes("409") ? "You already have a layout with that name." : e);
  const save = async () => {
    if (!picked) return saveAs();
    try {
      await api.updateSiteLayout(named.siteId, picked.id, { name: picked.name, config: current() });
      await named.reload();
      toast.success(`Layout "${picked.name}" saved`);
    } catch (e) { failed(e); }
  };
  const saveAs = async () => {
    setOpen(false);
    const name = (await promptDialog("Save layout as", { label: "Name, e.g. Doors or Perimeter" }))?.trim();
    if (!name) return;
    try {
      const l = await api.createSiteLayout(named.siteId, { name, config: current() });
      await named.reload();
      named.setLayoutId(l.id);
      toast.success(`Layout "${name}" saved`);
    } catch (e) { failed(e); }
  };
  const rename = async () => {
    setOpen(false);
    const name = picked && (await promptDialog("Rename layout", { initial: picked.name, confirmLabel: "Rename" }))?.trim();
    if (!picked || !name || name === picked.name) return;
    try {
      await api.updateSiteLayout(named.siteId, picked.id, { name, config: picked.config });
      await named.reload();
      toast.success(`Renamed to "${name}"`);
    } catch (e) { failed(e); }
  };
  const remove = async () => {
    setOpen(false);
    if (!picked || !await confirmDialog(`Delete the layout "${picked.name}"?`, { confirmLabel: "Delete", danger: true })) return;
    try {
      await api.deleteSiteLayout(named.siteId, picked.id);
      named.setLayoutId(0);   // what is on screen stays, as on the Timeline
      await named.reload();
      toast.success(`Deleted "${picked.name}"`);
    } catch (e) { failed(e); }
  };
  return (
    <>
      <span className="muted small">Layout</span>
      <LayoutSelect named={named} onPick={onPick} />
      {dirty && <span className="dirty-dot" title={picked ? "Unsaved changes to this layout" : "Changed from All cameras"}>●</span>}
      {picked ? <button className="ghost small" disabled={!dirty} onClick={save}>Save</button>
        : <button className="ghost small" onClick={saveAs} title="Save what the grid shows as a named layout">Save as…</button>}
      <div className="menu-anchor">
        <button className="ghost small" aria-haspopup="true" aria-expanded={open} aria-label="More layout actions" onClick={() => setOpen(!open)}>⋯</button>
        {open && (
          <>
            <div className="menu-veil" onClick={() => setOpen(false)} />
            <div className="menu-pop site-live-layout-menu" role="menu">
              <button role="menuitem" onClick={saveAs}>Save as new…</button>
              {picked && <button role="menuitem" onClick={rename}>Rename…</button>}
              {picked && <button role="menuitem" onClick={remove}>Delete…</button>}
              {dirty && <button role="menuitem" onClick={() => { setOpen(false); onRevert(); }}>{picked ? "Revert to saved" : "Back to all cameras"}</button>}
            </div>
          </>
        )}
      </div>
    </>
  );
}

/**
 * Drag a tile onto another to take its place (the Timeline's tile drag and visuals): from the ⠿ grip in the tile's bar,
 * so a drag on the picture still paints a region, pans a digital zoom or steers PTZ. The dragged tile dims, the one
 * under the pointer is outlined and a ghost with the camera's name follows the pointer; Esc cancels. Tiles move on
 * drop, not while dragging: each is a live video, and the grid doesn't reshuffle under the pointer.
 */
function useTileDrag(onDrop: (key: string, onto: string) => void) {
  const [dragKey, setDragKey] = useState<string | null>(null);
  const [dropKey, setDropKey] = useState<string | null>(null);
  const [ghost, setGhost] = useState<{ name: string; x: number; y: number } | null>(null);
  const dropRef = useRef(onDrop);
  dropRef.current = onDrop;
  const stop = useRef<(() => void) | null>(null);
  useEffect(() => () => stop.current?.(), []);
  const start = (key: string, name: string, e: React.PointerEvent) => {
    if (e.pointerType === "mouse" && e.button !== 0) return;
    e.preventDefault();
    e.stopPropagation();
    stop.current?.();
    let over: string | null = null;
    const move = (ev: PointerEvent) => {
      ev.preventDefault();
      setGhost({ name, x: ev.clientX, y: ev.clientY });
      const el = document.elementsFromPoint(ev.clientX, ev.clientY).find((x) => (x as HTMLElement).dataset?.liveKey) as HTMLElement | undefined;
      const k = el?.dataset.liveKey ?? null;
      over = k && k !== key ? k : null;
      setDropKey(over);
    };
    const up = () => {
      const target = over;
      stop.current?.();
      if (target) dropRef.current(key, target);
    };
    const esc = (ev: KeyboardEvent) => { if (ev.key === "Escape") cancel(); };
    const cancel = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", cancel);
      window.removeEventListener("keydown", esc);
      stop.current = null;
      setDragKey(null);
      setDropKey(null);
      setGhost(null);
    };
    stop.current = cancel;
    window.addEventListener("pointermove", move, { passive: false });
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", cancel);
    window.addEventListener("keydown", esc);
    setDragKey(key);
    setGhost({ name, x: e.clientX, y: e.clientY });
  };
  return { dragKey, dropKey, ghost, start };
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
 * each painted camera from its own server (only while the column shows: `withFeed`).
 */
function useSiteActivity(site: Site, servers: Server[], source: DashboardSource, enabled: boolean, withFeed: boolean): Activity {
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
  // the regions' contents too: repainting a camera refetches that camera only (the server matches the region, so a
  // burst of other events can't crowd the matches out of SCOPED_LIMIT; a server without that filter ignores it)
  const scoped = useRegionResults<FleetEvent>(enabled && withFeed ? regionFetchKey(regionKeys, regionMap) : "", (k, region) => {
    const { server, id } = splitKey(k);
    const name = serversRef.current.find((s) => s.id === server)?.name ?? server;
    return siteApi(server).events({ camera: id, region, status: "open,pending,verified", limit: SCOPED_LIMIT })
      .then((evs) => tagServer(evs, server, name)).catch(() => [] as FleetEvent[]);
  });

  const pool = useMemo(() => (scopeKey ? mergePool(scoped, recent ?? []) : recent ?? []), [scopeKey, scoped, recent]);
  return { recent, pool, regionKeys, regionMap, offline };
}

/** The "Latest activity" column: LiveView's feed and RegionNote, with the event viewer opened in place. */
function SiteActivity({ site, servers, tiles, multi, activity: a }: { site: Site; servers: Server[]; tiles: Tile[]; multi: boolean; activity: Activity }) {
  const [open, setOpen] = useState<EventRef | null>(null);
  const names = useMemo(() => new Map(tiles.map((t) => [t.key, t.name])), [tiles]);
  /** "Server · Camera" on a multi-server Site, as the tiles are labeled */
  const label = (key: string) => {
    const t = tiles.find((x) => x.key === key);
    const { server, id } = splitKey(key);
    return t ? tileLabel(t.server.name, t.name, multi) : tileLabel(servers.find((s) => s.id === server)?.name ?? server, id, multi);
  };
  const feed = regionFeed(a.pool, a.regionMap, a.regionKeys);
  const [shown, showMore] = useFeedCount(a.regionKeys.join(","));
  const offline = a.offline.map((id) => servers.find((s) => s.id === id)?.name ?? id);
  return (
    <>
      <aside className="live-feed">
        <h3>Latest activity</h3>
        <RegionChips items={a.regionKeys.map((k) => ({ key: k, label: label(k) }))} />
        {offline.length > 0 && <p className="muted small">Offline: {offline.join(", ")}</p>}
        {a.recent === null && <p className="muted">Loading…</p>}
        {a.recent !== null && a.pool.length === 0 && <p className="muted">Nothing yet.</p>}
        {a.pool.length > 0 && feed.length === 0 && <p className="muted">Nothing recent on that camera passed through the painted region.</p>}
        {feed.slice(0, shown).map((e) => (
          <EventCard key={`${e.site_id}-${e.id}`} e={e} cameraName={names.get(camKey(e.site_id, e.camera_id)) ?? e.camera_id}
            site={mediaApi(e.site_id)} siteName={multi ? e.site_name : undefined}
            onOpen={() => setOpen({ server: e.site_id, id: e.id, location: site.id })} />
        ))}
        <FeedMore shown={shown} total={feed.length} onMore={showMore} />
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

/**
 * "Cameras n/m ▾" in the toolbar: a popover listing every camera with an eye toggle (like the Timeline's lanes); drag
 * one onto another to reorder; Reset (back to the picked layout). A popover rather than a standing row of chips keeps
 * the Live tab to one toolbar.
 */
function CameraPicker({ tiles, layout, multi, dispatch, dirty, onRevert, revertLabel }: {
  tiles: Tile[]; layout: LiveLayout; multi: boolean; dispatch: React.Dispatch<LayoutAction>; dirty: boolean; onRevert: () => void; revertLabel: string;
}) {
  const all = tiles.map((t) => t.key);
  const [open, setOpen] = useState(false);
  const [drag, setDrag] = useState<string | null>(null);
  const shownCount = tiles.filter((t) => isVisible(layout, t.key)).length;
  return (
    <div className="menu-anchor">
      <button className={`ghost small cams-toggle ${shownCount < tiles.length ? "filtered" : ""}`} aria-expanded={open} aria-haspopup="true" onClick={() => setOpen(!open)}
        title="Choose which cameras show and in what order">
        Cameras {shownCount < tiles.length ? `${shownCount}/${tiles.length}` : tiles.length} ▾
      </button>
      {open && (
        <>
          <div className="menu-veil" onClick={() => setOpen(false)} />
          {/* stays open while toggling: several cameras are usually picked at once */}
          <div className="menu-pop cams-pop" role="group" aria-label="Cameras">
            <div className="muted small cams-hint">Click to show or hide · drag to reorder</div>
            {tiles.map((t) => {
              const on = isVisible(layout, t.key);
              return (
                <button key={t.key} className={`site-live-chip ${on ? "" : "hidden-cam"} ${drag === t.key ? "dragging" : ""}`} draggable aria-pressed={on}
                  title={`${on ? "Hide" : "Show"} ${t.name}${t.state === "offline" ? " (server offline)" : ""} · drag to reorder`}
                  onClick={() => dispatch({ type: "toggle", key: t.key, all })}
                  onDragStart={(e) => { setDrag(t.key); e.dataTransfer.effectAllowed = "move"; }}
                  onDragOver={(e) => { if (drag && drag !== t.key) e.preventDefault(); }}
                  onDrop={(e) => { e.preventDefault(); if (drag) dispatch({ type: "move", key: drag, before: t.key, all }); setDrag(null); }}
                  onDragEnd={() => setDrag(null)}>
                  <span aria-hidden="true" className="cams-eye">{on ? "👁" : "◌"}</span>
                  <span className={`dot ${t.state === "live" && t.streamReady ? "ok" : "bad"}`} />
                  <span className="cams-name">{tileLabel(t.server.name, t.name, multi)}</span>
                </button>
              );
            })}
            {dirty && <button className="cams-reset" onClick={() => { onRevert(); setOpen(false); }} title="Undo the changes since the layout was picked or saved">{revertLabel}</button>}
          </div>
        </>
      )}
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

/** A grid tile's part in a drag (useTileDrag): its grip, and whether it is the one dragged or the one under the pointer. */
type TileDrag = { onGrip: (e: React.PointerEvent) => void; dragging: boolean; dropTarget: boolean };

function SiteTile({ t, source, multi, q, dispatch, hdUnsupported, onUnsupported, activeOf, extra, phone, onSwipe, drag }: TileCommon & {
  t: Tile; extra?: React.ReactNode; phone?: boolean; onSwipe?: (dir: -1 | 1) => void; drag?: TileDrag;
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
  const grip = drag && <span className="sync-grip" title="Drag to reorder" onPointerDown={drag.onGrip}>⠿</span>;
  return (
    <div ref={box} className={`site-live-cell ${drag?.dragging ? "dragging" : ""} ${drag?.dropTarget ? "drop-target" : ""}`} data-live-key={t.key}>
      {playing && t.cam ? (
        <LiveTile c={t.cam} hd={hd} port={source.port} iceServers={ice} site={site} regionKey={t.key} phone={phone} onSwipe={onSwipe} active={active}
          onUnsupported={hd ? onUnsupported : undefined} bar={<>
            {grip}
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
            {grip}
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

/** Phone: one camera at a time, a strip of stills across every server's cameras, swipe or tap to switch (wraps).
 *  `picker` (the named layouts, when there are any) sets which cameras the strip has and their order. */
function PhoneLive({ tiles, activity, picker, ...common }: TileCommon & { tiles: Tile[]; activity: React.ReactNode; picker: React.ReactNode }) {
  const [idx, setIdx] = useState(0);
  const [tick, setTick] = useState(0);
  useEffect(() => { const t = setInterval(() => setTick((x) => x + 1), 10000); return () => clearInterval(t); }, []);
  const i = Math.min(idx, tiles.length - 1);
  const t = tiles[i];
  const step = (dir: -1 | 1) => setIdx(wrapIndex(i, dir, tiles.length));
  return (
    <LiveBudgetProvider>
      <div className="live-phone">
        {picker}
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
