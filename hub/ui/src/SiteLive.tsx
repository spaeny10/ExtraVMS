/**
 * The Site's combined Live view: one grid over every camera of every server at the Site, with the activity of all of
 * them alongside. Each tile talks to its own server through the hub tunnel (siteApi(server) = /s/<server>/…), with
 * that server's TURN relay, so WHEP offers for one grid go to several /s/<id>/ prefixes. When this browser is on a
 * server's LAN (direct.ts), that server's video, stills and snapshots come straight from it (mediaApi); writes such as
 * PTZ and painted regions still go through the hub.
 *
 * Streams share the page budget (MAX_LIVE, as on dashboards): tiles past it show a still with a Play button.
 * A server that is offline (or goes offline mid-view: the hub pushes it, SitePage useFleetRefresh) shows its cameras from
 * the hub's registry as "Server offline" tiles with the latest picture the hub keeps of each ("Last picture 14:05",
 * hub stills.py), since the server's own stills come through the tunnel; dark when there is none. That hub picture is
 * also each live tile's poster until the server's own still arrives.
 *
 * "Latest activity" mirrors the server UI's LiveView: event cards that open the event viewer in place (HubEventDetail),
 * filtered by a region painted on a tile (only that camera's events whose path crossed it), and tiles with an event
 * in progress marked as alerting. The pool and the filtering are eventOpen.ts. The Activity button hides the column
 * (the grid takes the full width).
 *
 * Layouts, as on the Timeline: "All cameras" or a named layout saved on the hub (per user and Site, spanning its
 * servers: hub/hub/layouts.py), with Save (shown while the grid differs from it) and, in the View menu, Save as new /
 * Rename / Delete / Revert. A
 * layout holds which cameras show and in what order, their streams, the grid's columns and the activity column. Tiles
 * reorder by dragging the ⠿ grip in their bar (desktop); the phone picks a layout only. The logic is liveLayout.ts.
 *
 * Streams: a tile is on Auto unless the viewer picked SD or HD for it (on the tile, All SD / All HD, or a saved layout
 * holding a pick): Auto goes HD on a large or expanded tile with a clean link, two at a time (LiveQuality.tsx), and
 * HD a browser can't play is greyed out per camera, from the server's codec list.
 *
 * Talk-down: a camera with speakers bound to it gets the 🔊 Talk-down button and panel on its live tile, for operators
 * and up (SOC operators too, here and in the SOC incident view that embeds this grid). A speaker is bound to cameras of
 * its own server (its heartbeat summary lists them) and/or to the Site's camera groups on the hub, which may hold
 * cameras of other servers (site/cameraGroups.ts: the union). Play and Stop go to the speaker's own server through the
 * hub (audited there), whatever server the camera is on; the panel reads its status from that server.
 * Deterrence: a camera with a strobe, siren or light gets the 🚨 button and panel on its tile for the same people
 * (canDeter); the calls go to the camera's own server through the hub.
 */
import { useCallback, useContext, useEffect, useId, useLayoutEffect, useMemo, useReducer, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { Camera as ServerCam, NvrEvent } from "@site/api";
import { EventCard } from "@site/Events";
import { FeedMore, LiveTile, RegionChips, useFeedCount } from "@site/Views";
import { QualityBadge } from "@site/LiveQuality";
import { LiveBudgetProvider, useBudget } from "@site/dashboard/Dashboard";
import { useIceServers } from "@site/dashboard/ice";
import { camKey, splitKey, type DashboardSource } from "@site/dashboard/source";
import type { FleetEvent } from "@site/dashboard/types";
import { NO_PICTURE_TEXT, stillDue } from "@site/liveFallback";
import { regionFetchKey, regionPass, useRegionResults, useRegions } from "@site/region";
import { useResync } from "@site/resync";
import type { TalkSpeaker } from "@site/talkdown";
import { Icon, confirmDialog, promptDialog, swipeHandlers, toast, useDismiss, useIsPhone } from "@site/ui";
import { useStill } from "@site/useStill";
import { type Camera as RegistryCam, type Fleet, type Org, type Server, type Site, type SiteCameraGroup, type SiteLayout, api, lastPictureText,
  stillUrl } from "./api";
import { type EventRef, applyLiveEvent, cameraNameFor, checkingText, feedCards, mergePool, regionFeed, removeLiveEvent, siteRegionKeys, tagServer } from "./eventOpen";
import { HubEventDetail } from "./HubEventDetail";
import { canDeter, canTalkDown } from "./access";
import { useDirect, useDirectVersion } from "./direct";
import { makeHubSource, mediaApi, siteApi } from "./hubSource";
import { talkByCamera } from "./site/cameraGroups";
import { TabToolsSlot } from "./tabTools";
import { COLUMN_CHOICES, EMPTY_LAYOUT, type LayoutAction, type LiveFocus, type LiveLayout, type QualityMode, applyFocus, arrange, coerceLayout, isDirty,
  isVisible, layoutCols, layoutReducer, loadActivityPref, loadLayout, loadLayoutId, saveActivityPref, saveLayout, saveLayoutId, tileLabel, tileQuality,
  toSavedConfig, viewSummary, wrapIndex } from "./liveLayout";

/** live: full camera record from the server · loading: server online, cameras not fetched yet · unreachable: the
 *  server is online at the hub but its camera list failed (busy tunnel, restarting) · offline: the server is offline */
type TileState = "live" | "loading" | "unreachable" | "offline";
type Tile = { key: string; server: Server; id: string; name: string; state: TileState; cam: ServerCam | null; streamReady: boolean;
  /** when the hub's latest picture of the camera was taken (stillUrl), null = none */
  stillTs: number | null };

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
  // the Site object is replaced on every refresh (SitePage useSite); a source rebuilt each time would make EventsWidget
  // refetch and re-open its socket. One source per Site, reading the latest servers through a ref, avoids that.
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
      return f.map((c) => ({ key: camKey(s.id, c.id), server: s, id: c.id, name: c.name, state: "live", cam: c, streamReady: !!c.status?.stream_ready,
        stillTs: s.stills?.[c.id] ?? null }));
    }
    const state: TileState = !s.online ? "offline" : f === "error" ? "unreachable" : "loading";
    const reg = registry.filter((r) => r.server_id === s.id && r.enabled && !r.missing_since);
    // a server that just dropped may not be in the registry fetch yet: keep showing the cameras we already know
    const known = reg.length ? reg.map((r) => ({ id: r.camera_id, name: r.name, still: r.still_ts })) : Array.isArray(f) ? f.map((c) => ({ id: c.id, name: c.name, still: null })) : [];
    return known.map((c) => ({ key: camKey(s.id, c.id), server: s, id: c.id, name: c.name, state, cam: null, streamReady: false,
      stillTs: s.stills?.[c.id] ?? c.still ?? null }));
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

  const q = (key: string): QualityMode => tileQuality(layout, key);
  const [expanded, setExpanded] = useState<string | null>(null);
  // Expand narrows the activity column to that camera too, until the viewer asks for everything again
  const [feedAll, setFeedAll] = useState(false);
  useEffect(() => { setFeedAll(false); }, [expanded]);
  const gridRef = useRef<HTMLDivElement>(null);
  const isPhone = useIsPhone();
  // the column shows unless the embed drops it (the SOC) or the viewer hid it (a phone has it under the picture: always)
  const showFeed = !hideActivity && (isPhone || layout.activity);
  // the SOC incident view hides the column (it has its own event list): no feed fetches or socket for it either. Hidden
  // by the viewer, the Site's recent events still mark alerting tiles; only the painted regions' fetches stop.
  const activity = useSiteActivity(site, servers, source, !hideActivity, showFeed, !isPhone && !feedAll ? expanded : null);
  const activeOf = (key: string) => activity.recent?.find((e) => camKey(e.site_id, e.camera_id) === key && (e.status === "open" || e.status === "pending"));

  // talk-down: camera key -> its speakers, direct (the Site's heartbeat summaries) and through camera groups
  const mayTalk = canTalkDown(org);
  const groups = useCameraGroups(site.id, mayTalk);
  const talk = useMemo(() => talkByCamera(servers, groups, siteApi), [servers, groups]);

  // above every early return below: hooks run in the same order every render
  const fit = useExpandFit(gridRef, !isPhone && !!expanded && shown.some((t) => t.key === expanded));
  // the Site page's tab row has room for the toolbar on its right (tabTools.ts); elsewhere it stays above the grid
  const slot = useContext(TabToolsSlot);
  if (servers.length === 0) return <p className="muted">This site has no servers yet.</p>;
  if (tiles.length === 0) return <p className="muted">{Object.keys(full).length || registry.length ? "No cameras at this site yet." : "Loading cameras…"}</p>;

  const tileProps = { source, multi, q, dispatch, activeOf,
    talkOf: (t: Tile) => (mayTalk ? talk[t.key] : undefined), deter: canDeter(org) };
  const feed = showFeed ? <SiteActivity site={site} servers={servers} tiles={tiles} multi={multi} activity={activity} onShowAll={() => setFeedAll(true)} /> : null;
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
  // the tab row's right side (or, without a Site page around, its own row): Layout, Save while changed, Activity, View
  const toolbar = !compact && (
    <div className="toolbar live-toolbar">
      {named.enabled && <LayoutPicker named={named} dirty={dirty} onPick={pickLayout} current={() => toSavedConfig(allKeys, layout)} />}
      {!hideActivity && (
        <button className={`ghost small ${layout.activity ? "on" : ""}`} aria-pressed={layout.activity} onClick={() => setActivity(!layout.activity)}
          title={layout.activity ? "Hide the Latest activity column: the cameras take the full width" : "Show the Latest activity column"}>
          Activity
        </button>
      )}
      <ViewMenu tiles={ordered} layout={layout} liveKeys={liveKeys} multi={multi} dispatch={dispatch} dirty={dirty} onRevert={revert}
        named={named.enabled ? named : null} current={() => toSavedConfig(allKeys, layout)} />
    </div>
  );
  return (
    <LiveBudgetProvider>
      {toolbar && slot !== undefined ? (slot ? createPortal(toolbar, slot) : null) : null}
      <div className={`live-layout ${showFeed ? "" : "site-live-solo"} ${compact ? "site-live-compact" : ""}`}>
        <div className="live-main">
          {slot === undefined && toolbar}
          <div ref={gridRef} className={`live-grid ${focused ? "expanded" : layoutCols(layout, grid.length) === 1 ? "one" : ""}`}
            style={{ gridTemplateColumns: `repeat(${focused ? 1 : layoutCols(layout, grid.length)}, minmax(0, 1fr))`,
              // a lone tile fits the window (styles.css .live-grid.one): the hub's header, tabs and toolbar above it
              ...({ "--live-chrome": "260px" } as React.CSSProperties),
              // an expanded tile on Auto goes HD (unless its HD can't play here): size the box for that
              ...(focused && fit ? { "--expand-h": `${fit}px`, "--expand-ar": q(focused.key) === "sd" ? 4 / 3 : 2592 / 1520 } as React.CSSProperties : {}) }}>
            {grid.map((t) => (
              <SiteTile key={t.key} t={t} {...tileProps} focused={!!focused}
                drag={draggable ? { onGrip: (e) => drag.start(t.key, tileLabel(t.server.name, t.name, multi), e), dragging: drag.dragKey === t.key,
                  dropTarget: drag.dropKey === t.key } : undefined}
                extra={<button type="button" className="ghost small icon-act" onClick={() => setExpanded(focused ? null : t.key)}
                  aria-label={focused ? "Back to the grid" : "Expand"} title={focused ? "Back to the grid" : "Expand this camera"}>
                  <Icon name={focused ? "grid" : "expand"} size={16} /></button>} />
            ))}
            {grid.length === 0 && <div className="empty">Every camera is hidden. Show some under View › Cameras, or Reset there.</div>}
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
 * The signed-in user's named layouts of this Site (the hub's /api/locations/{id}/layouts, shared with the Timeline and
 * re-read on every visit, so one saved, renamed or deleted there shows here) and which one is picked (this browser,
 * per Site; 0 = "All cameras"). `list` is null until the hub answers (or when it can't).
 */
/** The Site's camera groups with their bound speakers (re-read every minute: they change in Settings); [] until read,
 *  and on a hub that has none (only direct bindings then). */
function useCameraGroups(siteId: string, enabled: boolean): SiteCameraGroup[] {
  const [groups, setGroups] = useState<SiteCameraGroup[]>([]);
  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    const pull = () => api.cameraGroups(siteId).then((r) => { if (alive) setGroups(r.groups); }).catch(() => {});
    pull();
    const t = setInterval(pull, 60000);
    return () => { alive = false; clearInterval(t); };
  }, [siteId, enabled]);
  return groups;
}

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

/** Save, Save as new, Rename and Delete of the picked named layout (the toolbar's Save and the View menu's Layout part). */
function useLayoutActions(named: NamedLayouts, current: () => LiveLayout) {
  const { picked } = named;
  const failed = (e: unknown) => toast.error(String(e).includes("409") ? "You already have a layout with that name." : e);
  const saveAs = async () => {
    const name = (await promptDialog("Save layout as", { label: "Name, e.g. Doors or Perimeter" }))?.trim();
    if (!name) return;
    try {
      const l = await api.createSiteLayout(named.siteId, { name, config: current() });
      await named.reload();
      named.setLayoutId(l.id);
      toast.success(`Layout "${name}" saved`);
    } catch (e) { failed(e); }
  };
  const save = async () => {
    if (!picked) return saveAs();
    try {
      await api.updateSiteLayout(named.siteId, picked.id, { name: picked.name, config: current() });
      await named.reload();
      toast.success(`Layout "${picked.name}" saved`);
    } catch (e) { failed(e); }
  };
  const rename = async () => {
    const name = picked && (await promptDialog("Rename layout", { initial: picked.name, confirmLabel: "Rename" }))?.trim();
    if (!picked || !name || name === picked.name) return;
    try {
      await api.updateSiteLayout(named.siteId, picked.id, { name, config: picked.config });
      await named.reload();
      toast.success(`Renamed to "${name}"`);
    } catch (e) { failed(e); }
  };
  const remove = async () => {
    if (!picked || !await confirmDialog(`Delete the layout "${picked.name}"?`, { confirmLabel: "Delete", danger: true })) return;
    try {
      await api.deleteSiteLayout(named.siteId, picked.id);
      named.setLayoutId(0);   // what is on screen stays, as on the Timeline
      await named.reload();
      toast.success(`Deleted "${picked.name}"`);
    } catch (e) { failed(e); }
  };
  return { picked, save, saveAs, rename, remove };
}

/**
 * The toolbar's "Layout" part, as on the Timeline: the picker, and Save (or "Save as…" under All cameras) only while
 * the grid differs from the picked layout. Save as new, Rename, Delete and Revert are in the View menu.
 */
function LayoutPicker({ named, dirty, onPick, current }: {
  named: NamedLayouts; dirty: boolean; onPick: (id: number) => void;
  /** the arrangement on screen as a layout is saved (liveLayout.toSavedConfig) */
  current: () => LiveLayout;
}) {
  const { picked, save, saveAs } = useLayoutActions(named, current);
  return (
    <span className="live-layout-pick">
      <span className="muted small">Layout</span>
      <LayoutSelect named={named} onPick={onPick} />
      {dirty && (picked
        ? <button className="small" onClick={save} title={`Unsaved changes: save them to "${picked.name}"`}>Save</button>
        : <button className="small" onClick={saveAs} title="Changed from All cameras: save what the grid shows as a named layout">Save as…</button>)}
    </span>
  );
}

/**
 * "View ▾": what the grid shows and how, in one panel so the toolbar keeps to a single row: Stream (Auto / All SD /
 * All HD), the grid's columns, which cameras show and in what order (a list of eye toggles, drag to reorder, as on the
 * Timeline's lanes), and the named layout's Save as new / Rename / Delete / Revert. Stays open while picking (several
 * cameras are usually toggled at once); Escape or a click outside closes it.
 */
function ViewMenu({ tiles, layout, liveKeys, multi, dispatch, dirty, onRevert, named, current }: {
  tiles: Tile[]; layout: LiveLayout; liveKeys: string[]; multi: boolean; dispatch: React.Dispatch<LayoutAction>; dirty: boolean; onRevert: () => void;
  named: NamedLayouts | null; current: () => LiveLayout;
}) {
  const [open, setOpen] = useState(false);
  const [camsOpen, setCamsOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const btn = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const id = useId();
  useDismiss(box, open, () => setOpen(false), btn);
  useEffect(() => { if (open) panel.current?.focus(); }, [open]);
  const shownCount = tiles.filter((t) => isVisible(layout, t.key)).length;
  const v = viewSummary(layout, liveKeys, shownCount, tiles.length);
  const quality = (q: QualityMode) => dispatch({ type: "allQuality", keys: liveKeys, quality: q });
  // the panel closes before a dialog opens over it
  const then = (fn: () => void) => () => { setOpen(false); fn(); };
  return (
    <div ref={box} className="menu-anchor">
      <button ref={btn} className={`ghost small view-toggle ${v.filtered ? "filtered" : ""}`} aria-haspopup="dialog" aria-expanded={open}
        aria-controls={open ? id : undefined} onClick={() => setOpen(!open)} title={v.title}>
        View{v.filtered && <span className="muted"> · {v.filtered}</span>} <Icon name="chevronDown" size={14} />
      </button>
      {open && (
        <div ref={panel} id={id} className="hub-pop view-pop" role="dialog" aria-label="View" tabIndex={-1}>
          <div className="pop-sec">
            <span className="pop-h">Stream</span>
            <div className="segmented" role="group" aria-label="Stream quality for every camera">
              <button className={v.stream === "auto" ? "active" : ""} aria-pressed={v.stream === "auto"} onClick={() => quality("auto")}
                title="Each camera picks: HD while its tile is large and the link is clean (two at a time), else SD">Auto</button>
              <button className={v.stream === "sd" ? "active" : ""} aria-pressed={v.stream === "sd"} onClick={() => quality("sd")} title="Sub stream: low bandwidth">All SD</button>
              <button className={v.stream === "hd" ? "active" : ""} aria-pressed={v.stream === "hd"} onClick={() => quality("hd")}
                title="Full-resolution main stream (a camera whose HD this browser can't play stays on SD)">All HD</button>
            </div>
          </div>
          <label className="pop-sec">
            <span className="pop-h">Columns</span>
            <select className="site-live-cols" value={layout.cols ?? 0} onChange={(e) => dispatch({ type: "cols", cols: Number(e.target.value) || null })}>
              <option value={0}>Auto columns</option>
              {COLUMN_CHOICES.map((n) => <option key={n} value={n}>{n} column{n > 1 ? "s" : ""}</option>)}
            </select>
          </label>
          <div className="pop-sec pop-col">
            <button className={`pop-disclose ${v.filtered ? "filtered" : ""}`} aria-expanded={camsOpen} onClick={() => setCamsOpen(!camsOpen)}
              title="Choose which cameras show and in what order">
              <Icon name={camsOpen ? "chevronDown" : "chevronRight"} size={14} /> {v.cameras}
            </button>
            {camsOpen && <CameraList tiles={tiles} layout={layout} multi={multi} dispatch={dispatch} />}
          </div>
          {(named || dirty) && <LayoutActions named={named} dirty={dirty} onRevert={then(onRevert)} current={current} then={then} />}
        </div>
      )}
    </div>
  );
}

/** The View menu's Layout part: Save as new, Rename, Delete (a picked layout), and Revert while the grid differs. */
function LayoutActions({ named, dirty, onRevert, current, then }: {
  named: NamedLayouts | null; dirty: boolean; onRevert: () => void; current: () => LiveLayout; then: (fn: () => void) => () => void;
}) {
  const acts = useLayoutActions(named ?? NO_NAMED, current);
  const picked = named ? acts.picked : undefined;
  return (
    <div className="pop-sec pop-col pop-acts">
      <span className="pop-h">Layout{picked ? `: ${picked.name}` : ""}</span>
      <div className="row">
        {named && <button className="ghost small" onClick={then(acts.saveAs)}>Save as new…</button>}
        {picked && <button className="ghost small" onClick={then(acts.rename)}>Rename…</button>}
        {picked && <button className="ghost small" onClick={then(acts.remove)}>Delete…</button>}
        {dirty && (
          <button className="ghost small" onClick={onRevert} title="Undo the changes since the layout was picked or saved">
            {picked ? "Revert to saved" : named ? "Back to all cameras" : "Reset layout"}
          </button>
        )}
      </div>
    </div>
  );
}
/** Stands in for the named layouts where they are off (the hook runs either way; no action is offered then). */
const NO_NAMED: NamedLayouts = { siteId: "", enabled: false, list: null, layoutId: 0, setLayoutId: () => {}, reload: () => Promise.resolve(), picked: undefined };

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
  /** the expanded camera ("server/camera"): the column shows only its events (null: every camera) */
  focusKey: string | null;
};

/**
 * LiveView's `recent + socket` pool and its painted-region fetches, for every server at the Site: the Site's latest
 * events from the hub, live updates from the fleet socket (this Site's servers only), and up to 100 recent events of
 * each painted camera from its own server (only while the column shows: `withFeed`).
 */
function useSiteActivity(site: Site, servers: Server[], source: DashboardSource, enabled: boolean, withFeed: boolean, focusKey: string | null = null): Activity {
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

  // back from the background (or the network): read the list again and REPLACE what the page holds — updates the
  // socket missed meanwhile (server restarts, a sleeping phone) left cards "Waiting for verification…" for hours
  const resync = useResync();
  useEffect(() => {
    if (!enabled || !resync) return;
    let alive = true;
    api.locationEvents(site.id, { limit: RECENT_LIMIT })
      .then((r) => { if (alive) { setRecent(r.events); setOffline(r.offline); } })
      .catch(() => {});
    return () => { alive = false; };
  }, [enabled, site.id, resync]);

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

  // the expanded camera's own deeper history: the Site's recent list may hold only a few of its events
  const [focused, setFocused] = useState<FleetEvent[]>([]);
  useEffect(() => {
    setFocused([]);
    if (!enabled || !withFeed || !focusKey) return;
    let alive = true;
    const { server, id } = splitKey(focusKey);
    const name = serversRef.current.find((s) => s.id === server)?.name ?? server;
    siteApi(server).events({ camera: id, status: "open,pending,verified", limit: SCOPED_LIMIT })
      .then((evs) => { if (alive) setFocused(tagServer(evs, server, name)); }).catch(() => {});
    return () => { alive = false; };
  }, [enabled, withFeed, focusKey, resync]);

  const pool = useMemo(() => {
    const deeper = [...(scopeKey ? scoped : []), ...focused];
    return deeper.length ? mergePool(deeper, recent ?? []) : recent ?? [];
  }, [scopeKey, scoped, focused, recent]);
  return { recent, pool, regionKeys, regionMap, offline, focusKey };
}

/** Expand: the height left for the one picture below the grid's top edge, so the expanded camera (picture plus its
 *  name bar) fits the window instead of growing with the page width past the bottom of the screen. */
const EXPAND_BAR_PX = 56;   // the tile's name bar and the gap under it
function useExpandFit(grid: React.RefObject<HTMLDivElement | null>, on: boolean): number | null {
  const [h, setH] = useState<number | null>(null);
  useLayoutEffect(() => {
    if (!on) { setH(null); return; }
    const measure = () => {
      const el = grid.current;
      if (!el) return;
      const top = el.getBoundingClientRect().top + window.scrollY;   // from the top of the page, not of the viewport
      setH(Math.max(200, Math.floor(window.innerHeight - top - EXPAND_BAR_PX)));
    };
    measure();
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, [grid, on]);
  return h;
}

/** The "Latest activity" column: LiveView's feed and RegionNote, with the event viewer opened in place. */
function SiteActivity({ site, servers, tiles, multi, activity: a, onShowAll }: {
  site: Site; servers: Server[]; tiles: Tile[]; multi: boolean; activity: Activity; onShowAll?: () => void;
}) {
  const [open, setOpen] = useState<EventRef | null>(null);
  const names = useMemo(() => new Map(tiles.map((t) => [t.key, t.name])), [tiles]);
  /** "Server · Camera" on a multi-server Site, as the tiles are labeled */
  const label = (key: string) => {
    const t = tiles.find((x) => x.key === key);
    const { server, id } = splitKey(key);
    return t ? tileLabel(t.server.name, t.name, multi) : tileLabel(servers.find((s) => s.id === server)?.name ?? server, id, multi);
  };
  const fk = a.focusKey;
  // expanded: that camera's events only (through its painted region, if it has one); otherwise the painted cameras'
  const feed = fk
    ? a.pool.filter((e) => camKey(e.site_id, e.camera_id) === fk && (!a.regionMap[fk] || regionPass(e, a.regionMap[fk])))
    : regionFeed(a.pool, a.regionMap, a.regionKeys);
  const { cards, checking } = feedCards(feed);   // unverified vehicles: counted, not shown (activeOf still sees them)
  const [shown, showMore] = useFeedCount(`${a.regionKeys.join(",")}|${fk ?? ""}`);
  const offline = a.offline.map((id) => servers.find((s) => s.id === id)?.name ?? id);
  return (
    <>
      <aside className="live-feed">
        <h3>Latest activity</h3>
        {checking > 0 && <p className="muted small">{checkingText(checking)}</p>}
        {fk && (
          <p className="muted small">Only {label(fk)} ·{" "}
            <button type="button" className="linkish" onClick={onShowAll}>Show all cameras</button></p>
        )}
        <RegionChips items={a.regionKeys.map((k) => ({ key: k, label: label(k) }))} />
        {offline.length > 0 && <p className="muted small">Offline: {offline.join(", ")}</p>}
        {a.recent === null && <p className="muted">Loading…</p>}
        {a.recent !== null && a.pool.length === 0 && <p className="muted">Nothing yet.</p>}
        {a.pool.length > 0 && feed.length === 0 && <p className="muted">{fk && !a.regionMap[fk] ? "Nothing recent on this camera." : "Nothing recent on that camera passed through the painted region."}</p>}
        {cards.slice(0, shown).map((e) => (
          <EventCard key={`${e.site_id}-${e.id}`} e={e} cameraName={names.get(camKey(e.site_id, e.camera_id)) ?? e.camera_id}
            site={mediaApi(e.site_id)} siteName={multi ? e.site_name : undefined}
            onOpen={() => setOpen({ server: e.site_id, id: e.id, location: site.id })} />
        ))}
        <FeedMore shown={shown} total={cards.length} onMore={showMore} />
      </aside>
      {/* a sibling of the column, as in LiveView, so the column's scroll and card styles don't reach the viewer */}
      {open && <HubEventDetail ev={open} cameraName={cameraNameFor(names, open.server)} onClose={() => setOpen(null)} />}
    </>
  );
}

type TileCommon = {
  source: DashboardSource; multi: boolean; q: (key: string) => QualityMode; dispatch: React.Dispatch<LayoutAction>;
  /** the camera's event in progress (open or pending), as LiveView marks a tile alerting */
  activeOf: (key: string) => NvrEvent | undefined;
  /** the speakers this viewer may talk through on the tile's camera (none: no Talk-down button) */
  talkOf: (t: Tile) => TalkSpeaker[] | undefined;
  /** may switch the cameras' deterrence outputs (the button shows only on a camera that has some) */
  deter: boolean;
};

/**
 * The View menu's camera list: every camera with an eye toggle (like the Timeline's lanes); drag one onto another to
 * reorder. A list in the menu rather than a standing row of chips keeps the Live tab to one toolbar.
 */
function CameraList({ tiles, layout, multi, dispatch }: { tiles: Tile[]; layout: LiveLayout; multi: boolean; dispatch: React.Dispatch<LayoutAction> }) {
  const all = tiles.map((t) => t.key);
  const [drag, setDrag] = useState<string | null>(null);
  return (
    <div className="cams-list" role="group" aria-label="Cameras">
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
            <span aria-hidden="true" className="cams-eye">{on ? <Icon name="eye" size={14} /> : <span className="cams-off" />}</span>
            <span className={`dot ${t.state === "live" && t.streamReady ? "ok" : "bad"}`} />
            <span className="cams-name">{tileLabel(t.server.name, t.name, multi)}</span>
          </button>
        );
      })}
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

function SiteTile({ t, source, multi, q, dispatch, activeOf, talkOf, deter, extra, phone, onSwipe, drag, focused = false }: TileCommon & {
  t: Tile; extra?: React.ReactNode; phone?: boolean; onSwipe?: (dir: -1 | 1) => void; drag?: TileDrag;
  /** the tile is the expanded one */
  focused?: boolean;
}) {
  const box = useRef<HTMLDivElement>(null);
  const budget = useBudgetSlot(t.key, box);
  // TURN credentials are minted per server; until they arrive the tile shows a still rather than connecting twice
  const ice = useIceServers(source, t.server.id);
  useDirect(t.server);  // re-render when this server's media route changes (the tile reconnects on the new client)
  const site = mediaApi(t.server.id);
  const mode = q(t.key);
  const pick = (m: QualityMode) => dispatch({ type: "quality", key: t.key, quality: m });
  const playing = t.state === "live" && (phone || budget.playing(t.key)) && ice !== undefined;
  // every 10 s while paused, every 60 s after a still failed to load (the server's own, through the tunnel)
  const still = useStill(!playing && t.state === "live");
  // the hub's latest picture: the live tile's poster until the server's still arrives, and the picture of a tile whose
  // server is offline or not answering
  const hubStill = stillUrl(t.server.id, t.id, t.stillTs);
  const label = tileLabel(t.server.name, t.name, multi);
  const active = activeOf(t.key);
  const grip = drag && <span className="sync-grip" title="Drag to reorder" onPointerDown={drag.onGrip}>⠿</span>;
  return (
    <div ref={box} className={`site-live-cell ${focused ? "focused" : ""} ${drag?.dragging ? "dragging" : ""} ${drag?.dropTarget ? "drop-target" : ""}`} data-live-key={t.key}>
      {playing && t.cam ? (
        // the tidy bar: the name (full in its tooltip) and a status dot; the quality badge and the actions show on
        // hover or keyboard focus on a desktop, always on a touch screen (hub.css .tile-acts)
        <LiveTile c={t.cam} quality={mode} onQuality={pick} focused={focused} port={source.port} iceServers={ice} site={site} regionKey={t.key} phone={phone} poster={hubStill}
          onSwipe={onSwipe} active={active} speakers={talkOf(t)} deter={deter} tidy actions={extra} bar={() => <>
            {grip}
            <span className={`dot ${t.streamReady ? "ok" : "bad"}`} title={t.streamReady ? "Recording" : "No stream"} />
            <span className="tile-name" title={label}>{label}</span>
            {active && <span className={`label-chip ${active.camera_class}`}>{active.camera_class}</span>}
            <span className="spacer" />
          </>} />
      ) : (
        <div className={`tile ${t.state === "live" ? "" : "site-live-down"}`} {...(phone && onSwipe ? swipeHandlers(onSwipe) : {})}>
          <div className="player dash-still">
            {t.state === "live"
              ? <img src={site.frameUrl(t.id, still.ts, 640)} alt="" onLoad={still.onLoad} onError={still.onError} style={still.failed ? { visibility: "hidden" } : undefined} />
              : hubStill && t.stillTs
                ? <><img src={hubStill} alt="" /><div className="player-badge stale" role="status">{STATE_TEXT[t.state]} · {lastPictureText(t.stillTs)}</div></>
                : <div className="player-state">{STATE_TEXT[t.state]}</div>}
            {t.state === "live" && still.failed && <div className="player-state">{NO_PICTURE_TEXT}</div>}
            {t.state === "live" && (
              <button className="dash-play" onClick={() => budget.force(t.key)} title="Play live (another tile pauses if the page is at its limit)"><Icon name="play" size={22} /></button>
            )}
          </div>
          <div className="tile-bar tile-bar-tidy">
            {grip}
            <span className={`dot ${t.state === "live" && t.streamReady ? "ok" : "bad"}`} title={t.state === "live" ? (t.streamReady ? "Recording" : "No stream") : STATE_TEXT[t.state]} />
            <span className="tile-name" title={label}>{label}</span>
            {t.state === "live" && <span className="muted small tile-state" title="Paused: a still frame (the page plays a limited number of cameras at once)">paused</span>}
            {t.state !== "live" && t.state !== "loading" && <span className="muted small tile-state">{t.state === "offline" ? "offline" : "unreachable"}</span>}
            <span className="spacer" />
            <span className="tile-acts">
              {t.state === "live" && <QualityBadge mode={mode} hd={mode === "hd"} onPick={pick} live={false} />}
              {extra}
            </span>
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
  // thumbnails whose still failed, by the tick it failed on: they try again once a minute instead of every tick
  const [failedAt, setFailedAt] = useState<Record<string, number>>({});
  const stillFailed = (key: string, failed: boolean) => setFailedAt((f) => {
    if (failed) return { ...f, [key]: tick };
    if (!(key in f)) return f;
    const rest = { ...f };
    delete rest[key];
    return rest;
  });
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
                {x.state !== "live" ? (x.stillTs
                  ? <img className="site-live-strip-stale" src={stillUrl(x.server.id, x.id, x.stillTs)!} alt="" title={`Offline · ${lastPictureText(x.stillTs)}`} />
                  : <div className="site-live-strip-off">offline</div>) : <>
                  {stillDue(tick, failedAt[x.key]) && (
                    <img src={`${mediaApi(x.server.id).frameUrl(x.id, Date.now() / 1000 - 15, 320)}&r=${tick}`} alt=""
                      onLoad={() => stillFailed(x.key, false)} onError={() => stillFailed(x.key, true)} style={x.key in failedAt ? { display: "none" } : undefined} />
                  )}
                  {x.key in failedAt && <div className="site-live-strip-off">{NO_PICTURE_TEXT}</div>}
                </>}
                <span><span className={`dot ${x.state === "live" && x.streamReady ? "ok" : "bad"}`} /> {tileLabel(x.server.name, x.name, common.multi)}</span>
              </button>
            ))}
          </div>
        )}
        {tiles.length > 1 && <p className="muted small center">Swipe the picture or tap a thumbnail to switch cameras.</p>}
        {activity}
      </div>
    </LiveBudgetProvider>
  );
}
