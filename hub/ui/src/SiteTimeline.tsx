/**
 * The Site's combined Timeline: the server UI's TimelineView over every camera of every online server at the Site.
 * Each lane's recordings, previews, locks and event details go to its own server through the hub tunnel
 * (apiFor = siteApi → /s/<server>/…); lane keys are camKey(server, camera) and each server's clock skew is applied.
 * Named layouts are kept in this browser per Site until the hub stores them (timelineLink.localLayoutStore).
 *
 * Direct-on-LAN (direct.ts): a server this browser reaches on its LAN plays from the server itself (mediaFor =
 * mediaApi), at full quality with the server UI's local chunking. Servers still reached through the hub play the SD
 * transcode by default when they offer one (SD/HD in the toolbar, kept per Site), and a first hub visit to a Site with
 * more than two cameras starts on one camera (timelineLink.initialTimelineLayout).
 *
 * Deep link (timelineLink.ts): ?server=&cam=&event=[&journey=1] focuses an event, ?server=&cam=&t= a moment.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { PlaybackQuality } from "@site/api";
import type { Camera as ServerCam } from "@site/api";
import { NavContext, type TimelineFocus, type TimelineTarget } from "@site/nav";
import { camKey } from "@site/playback";
import { toast } from "@site/ui";
import { decodeCells, regions } from "@site/region";
import { TimelineView, type TimelineCamera } from "@site/Timeline";
import type { Org, Server, Site } from "./api";
import { directStateOf, useDirectVersion } from "./direct";
import { isDirect, mediaApi, siteApi } from "./hubSource";
import { effectiveOffset, focusFor, fromLaneKeys, initialTimelineLayout, journeyMembers, localLayoutStore, momentTarget, parseSiteTimelineQuery,
  siteTimelineHref, withoutTimelineParams, type StoredLayoutConfig } from "./timelineLink";
import "./siteTimeline.css";

const CAMERA_REFRESH_MS = 60000;
/** the Timeline waits this long at most for the Direct-on-LAN checks before choosing its first layout */
const DIRECT_WAIT_MS = 6000;
const HUB_SOLO_HINT = "Playing one camera via the hub · press 0 for the grid";
/** a lane reached through the hub (slow uplink: short chunks, patient buffering); direct lanes play like the server UI */
const viaHub = (server: string) => !isDirect(server);

/** Can the server transcode playback to SD (q=sd)? Asked once per server per page load; an older server says no. */
const sdCaps = new Map<string, Promise<boolean>>();
const canSd = (server: string): Promise<boolean> => {
  let p = sdCaps.get(server);
  if (!p) {
    p = siteApi(server).playbackCapabilities().then((c) => !!c?.sd).catch(() => false);
    sdCaps.set(server, p);
  }
  return p;
};
const dropHashLink = (hash: string) => (hash.startsWith("#timeline") ? "" : hash);

/** ICE servers (the hub's TURN relay) per server for the live tiles at the live edge; asked once per server per page load. */
const iceCache = new Map<string, Promise<RTCIceServer[]>>();
const iceFor = (server: string): Promise<RTCIceServer[]> => {
  let p = iceCache.get(server);
  if (!p) {
    p = siteApi(server).turn().then((t) => t.iceServers ?? []).catch(() => [] as RTCIceServer[]);
    iceCache.set(server, p);
  }
  return p;
};

/**
 * `syncUrl` (default true): focusing an event or clearing focus rewrites the page URL into a shareable Timeline link.
 * An embedding page whose URL means something else (the SOC's /soc/incidents/:id) passes false and deep-links
 * through `query` instead (same parameters as the link: ?server=&cam=&event=).
 * `canRecoverSd`: offer "recover from the SD card" on gaps (admins; the proxy requires admin for POST /api/sd/recover).
 */
export function SiteTimeline({ site, query, syncUrl = true, canRecoverSd = false }: {
  org: Org; site: Site; query: string | URLSearchParams; syncUrl?: boolean; canRecoverSd?: boolean;
}) {
  const servers = useMemo(() => site.servers.filter((s) => !s.retired_at), [site.servers]);
  const online = useMemo(() => servers.filter((s) => s.online), [servers]);
  const onlineKey = online.map((s) => s.id).sort().join(",");
  // the Site object is replaced on every poll: read the latest servers (names, clock skew) through a ref
  const serversRef = useRef<Server[]>(servers);
  serversRef.current = servers;

  // ---- cameras of each online server (enabled only); reloaded when a server comes or goes, and every minute
  const [byServer, setByServer] = useState<Record<string, ServerCam[]>>({});
  const [failed, setFailed] = useState<Record<string, boolean>>({});
  useEffect(() => {
    const ids = onlineKey ? onlineKey.split(",") : [];
    let alive = true;
    const pull = () => ids.forEach((id) => siteApi(id).cameras()
      .then((cams) => {
        if (!alive) return;
        setByServer((m) => ({ ...m, [id]: cams.filter((c) => c.enabled) }));
        setFailed((f) => ({ ...f, [id]: false }));
      })
      .catch(() => { if (alive) setFailed((f) => ({ ...f, [id]: true })); }));
    pull();
    const t = setInterval(pull, CAMERA_REFRESH_MS);
    return () => { alive = false; clearInterval(t); };
  }, [onlineKey]);

  // lanes in Site order (servers as the Site lists them, cameras as each server lists them); offline servers are left
  // out (a lane needs its server for recordings). Rebuilt only when a camera, a server name or a clock skew changes,
  // so the Site's 15 s poll and the minute refresh don't make TimelineView reload every lane.
  const lanesSig = online.map((s) => `${s.id}:${s.name}:${s.clock_skew_s ?? 0}=${JSON.stringify(byServer[s.id] ?? null)}`).join(";");
  const cameras = useMemo<TimelineCamera[]>(() => online.flatMap((s) => (byServer[s.id] ?? []).map((c) => ({
    ...c, key: camKey(s.id, c.id), server: s.id, serverName: s.name, timeOffsetS: s.clock_skew_s ?? 0,
  })),
  // eslint-disable-next-line react-hooks/exhaustive-deps
  ), [lanesSig]);
  // TimelineView reads its stored config when it mounts: wait for every online server's first answer (or failure)
  const settled = online.every((s) => byServer[s.id] !== undefined || failed[s.id]);

  const storageKey = `timeline.${site.id}.`;
  const layoutStore = useMemo(() => localLayoutStore(site.id), [site.id]);

  // ---- Direct-on-LAN: which servers' media comes straight from them (re-rendered when that changes)
  useDirectVersion(online);
  const [directWaitOver, setDirectWaitOver] = useState(false);
  useEffect(() => { const t = setTimeout(() => setDirectWaitOver(true), DIRECT_WAIT_MS); return () => clearTimeout(t); }, [site.id]);
  const directSettled = directWaitOver || online.every((s) => directStateOf(s) !== "checking");
  const anyHub = online.some((s) => viaHub(s.id));

  // ---- quality for lanes still on the hub: SD (the server's transcode) by default where offered; kept per Site
  const qualityKey = `${storageKey}playbackQuality`;
  const [quality, setQualityState] = useState<PlaybackQuality>(() => {
    try { return localStorage.getItem(qualityKey) === "hd" ? "hd" : "sd"; } catch { return "sd"; }
  });
  const setQuality = useCallback((q: PlaybackQuality) => {
    setQualityState(q);
    try { localStorage.setItem(qualityKey, q); } catch { /* private mode: this visit only */ }
  }, [qualityKey]);
  const [sd, setSd] = useState<Record<string, boolean>>({});
  const [ice, setIce] = useState<Record<string, RTCIceServer[]>>({});
  useEffect(() => {
    let alive = true;
    (onlineKey ? onlineKey.split(",") : []).forEach((id) => {
      canSd(id).then((ok) => { if (alive) setSd((m) => (m[id] === ok ? m : { ...m, [id]: ok })); });
      iceFor(id).then((v) => { if (alive) setIce((m) => (m[id] ? m : { ...m, [id]: v })); });
    });
    return () => { alive = false; };
  }, [onlineKey]);
  const iceServersFor = useCallback((server: string) => ice[server], [ice]);
  // servers that answered 503 to an SD chunk (transcode slots full): HD for the rest of this visit, not persisted
  const [sdBusy, setSdBusy] = useState<Record<string, boolean>>({});
  const busyRef = useRef<Set<string>>(new Set());   // several tiles of one server can hit it at once: toast once
  const onSdBusy = useCallback((server: string) => {
    if (busyRef.current.has(server)) return;
    busyRef.current.add(server);
    toast.info(`${serversRef.current.find((s) => s.id === server)?.name ?? "The server"} is busy with other SD streams; playing HD instead`);
    setSdBusy((b) => ({ ...b, [server]: true }));
  }, []);
  const qualityFor = (server: string): PlaybackQuality | undefined => (viaHub(server) && sd[server] && !sdBusy[server] ? quality : undefined);
  const hubSd = online.some((s) => viaHub(s.id) && sd[s.id]);

  // first visit to a big Site: at most 8 lanes per server; through the hub, more than two cameras start on one
  // (only when nothing is stored yet; an automatic solo is undone once the Site is reached directly)
  const [readyFor, setReadyFor] = useState<string | null>(null);
  const [hubSolo, setHubSolo] = useState(false);
  const ready = readyFor === site.id;
  const search = typeof query === "string" ? query : `?${query.toString()}`;
  useEffect(() => {
    if (ready || !settled || !cameras.length || !directSettled) return;
    const k = `${storageKey}timelineLayoutConfig`, flagKey = `${storageKey}timelineHubSolo`;
    let soloed = false;
    try {
      const raw = localStorage.getItem(k);
      const stored = raw == null ? null : (JSON.parse(raw) as StoredLayoutConfig | null);
      const target = parseSiteTimelineQuery(search, location.hash);
      const focusServer = target?.server ?? (online.length === 1 ? online[0].id : null);
      const focusKey = target?.cam && focusServer ? camKey(focusServer, target.cam) : null;
      const next = initialTimelineLayout({
        stored: stored && typeof stored === "object" ? stored : null, flag: localStorage.getItem(flagKey),
        cams: cameras.map((c) => ({ key: c.key!, server: c.server! })), focusKey, viaHub: anyHub,
      });
      if (next.config) localStorage.setItem(k, JSON.stringify(next.config));
      if (next.flag === null) localStorage.removeItem(flagKey);
      else if (next.flag) localStorage.setItem(flagKey, next.flag);
      soloed = anyHub && !!localStorage.getItem(flagKey);
    } catch { /* private mode: TimelineView shows every lane */ }
    setHubSolo(soloed);
    setReadyFor(site.id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, settled, cameras, storageKey, site.id, directSettled]);

  // ---- focus: from the deep link, or from an event viewer's "Open in Timeline"
  const [focus, setFocus] = useState<TimelineFocus | null>(null);
  const offsetOf = (server: string) => effectiveOffset(serversRef.current.find((s) => s.id === server)?.clock_skew_s);
  /** Focus a server's event or moment in place, and keep the URL a shareable link to it. */
  const focusOn = useCallback((server: string, e: TimelineTarget) => {
    setFocus(focusFor(server, e, offsetOf(server)));
    const journey = !!e.members?.length;
    if (!syncUrl) return;
    const href = siteTimelineHref(site.id, server, journey ? e.members![0].cam : e.camera_id, e.id || null, e.id ? null : e.start_ts, journey);
    history.replaceState(history.state, "", href + dropHashLink(location.hash));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [site.id, syncUrl]);

  useEffect(() => {
    const target = parseSiteTimelineQuery(search, location.hash);
    if (!target?.cam) return;
    // a link without &server= names a camera of the Site's only (else first online) server
    const all = serversRef.current;
    const server = target.server ?? (all.length === 1 ? all[0].id : all.find((s) => s.online)?.id);
    if (!server) return;
    if (target.region) regions.set(camKey(server, target.region.cam), decodeCells(target.region.cells));
    let alive = true;
    if (target.event) {
      const id = target.event, s = siteApi(server);
      // a journey link restores all its sightings, not just the one event
      Promise.all([s.event(id), target.journey ? s.eventJourney(id).catch(() => null) : null])
        .then(([e, j]) => { if (alive) setFocus(focusFor(server, j ? { ...e, members: journeyMembers(j.events) } : e, offsetOf(server))); })
        .catch(() => {});
    } else if (target.t) setFocus(focusFor(server, momentTarget(target.cam, target.t), offsetOf(server)));
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search, site.id]);

  const clearFocus = useCallback(() => {
    setFocus(null);
    if (syncUrl) history.replaceState(history.state, "", location.pathname + withoutTimelineParams(location.search) + dropHashLink(location.hash));
  }, [syncUrl]);

  // An event viewer's "Open in Timeline": TimelineView passes lane keys (camKey(server, camera)), which carry the server
  const openInTimeline = useCallback((e: TimelineTarget) => {
    const hit = fromLaneKeys(e);
    if (hit) focusOn(hit.server, hit.target);
  }, [focusOn]);
  const nav = useMemo(() => ({ openInTimeline }), [openInTimeline]);

  if (servers.length === 0) return <p className="muted">This site has no servers yet.</p>;
  if (online.length === 0) {
    return <p className="muted">Every server at this site is offline. Recordings are reached through their server, so the Timeline is back when one reconnects.</p>;
  }
  if (!settled || (cameras.length > 0 && !ready)) return <p className="muted">{settled && !directSettled ? "Checking for a direct connection…" : "Loading cameras…"}</p>;
  if (!cameras.length) {
    return <p className="muted">{online.every((s) => failed[s.id]) ? "The servers at this site aren't answering right now. Retrying…" : "No cameras at this site yet."}</p>;
  }
  const offline = servers.filter((s) => !s.online);
  const unreachable = online.filter((s) => failed[s.id] && !byServer[s.id]);
  const missing = [...offline, ...unreachable];
  const why = offline.length && unreachable.length ? "offline or not answering" : offline.length ? "offline" : "not answering";
  return (
    <NavContext.Provider value={nav}>
      {missing.length > 0 && <p className="muted small site-timeline-note">Not shown: {missing.map((s) => s.name).join(", ")} ({why}).</p>}
      <TimelineView key={site.id} cameras={cameras} focus={focus} onClearFocus={clearFocus} apiFor={siteApi} mediaFor={mediaApi} remote={viaHub}
        qualityFor={qualityFor} onQualityUnavailable={onSdBusy} layoutStore={layoutStore} storageKey={storageKey} iceFor={iceServersFor}
        qualityToggle={hubSd ? { value: quality, set: setQuality, title: "Playback quality for cameras reached through the hub: SD is a 720p low-bitrate copy that starts faster on a slow uplink" } : undefined}
        soloHint={hubSolo && anyHub ? HUB_SOLO_HINT : undefined} canRecoverSd={canRecoverSd} />
    </NavContext.Provider>
  );
}
