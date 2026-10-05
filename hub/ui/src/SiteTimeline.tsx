/**
 * The Site's combined Timeline: the server UI's TimelineView over every camera of every online server at the Site.
 * Each lane's recordings, previews, locks and event details go to its own server through the hub tunnel
 * (apiFor = siteApi → /s/<server>/…); lane keys are camKey(server, camera) and each server's clock skew is applied.
 * Named layouts are kept in this browser per Site until the hub stores them (timelineLink.localLayoutStore).
 *
 * Deep link (timelineLink.ts): ?server=&cam=&event=[&journey=1] focuses an event, ?server=&cam=&t= a moment.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Camera as ServerCam } from "@site/api";
import { NavContext, type TimelineFocus, type TimelineTarget } from "@site/nav";
import { camKey } from "@site/playback";
import { decodeCells, regions } from "@site/region";
import { TimelineView, type TimelineCamera } from "@site/Timeline";
import type { Org, Server, Site } from "./api";
import { siteApi } from "./hubSource";
import { defaultVisible, effectiveOffset, focusFor, fromLaneKeys, journeyMembers, localLayoutStore, momentTarget, parseSiteTimelineQuery,
  siteTimelineHref, withoutTimelineParams } from "./timelineLink";
import "./siteTimeline.css";

const CAMERA_REFRESH_MS = 60000;
const dropHashLink = (hash: string) => (hash.startsWith("#timeline") ? "" : hash);

export function SiteTimeline({ site, query }: { org: Org; site: Site; query: string | URLSearchParams }) {
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

  // first visit to a big Site: at most 8 lanes per server (only when nothing is stored yet)
  const [readyFor, setReadyFor] = useState<string | null>(null);
  const ready = readyFor === site.id;
  useEffect(() => {
    if (ready || !settled || !cameras.length) return;
    const k = `${storageKey}timelineLayoutConfig`;
    try {
      if (localStorage.getItem(k) == null) {
        const visible = defaultVisible(cameras.map((c) => ({ key: c.key!, server: c.server! })));
        if (visible) localStorage.setItem(k, JSON.stringify({ visible, solo: null, order: null }));
      }
    } catch { /* private mode: TimelineView shows every lane */ }
    setReadyFor(site.id);
  }, [ready, settled, cameras, storageKey, site.id]);

  // ---- focus: from the deep link, or from an event viewer's "Open in Timeline"
  const [focus, setFocus] = useState<TimelineFocus | null>(null);
  const offsetOf = (server: string) => effectiveOffset(serversRef.current.find((s) => s.id === server)?.clock_skew_s);
  /** Focus a server's event or moment in place, and keep the URL a shareable link to it. */
  const focusOn = useCallback((server: string, e: TimelineTarget) => {
    setFocus(focusFor(server, e, offsetOf(server)));
    const journey = !!e.members?.length;
    const href = siteTimelineHref(site.id, server, journey ? e.members![0].cam : e.camera_id, e.id || null, e.id ? null : e.start_ts, journey);
    history.replaceState(history.state, "", href + dropHashLink(location.hash));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [site.id]);

  const search = typeof query === "string" ? query : `?${query.toString()}`;
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
    history.replaceState(history.state, "", location.pathname + withoutTimelineParams(location.search) + dropHashLink(location.hash));
  }, []);

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
  if (!settled || (cameras.length > 0 && !ready)) return <p className="muted">Loading cameras…</p>;
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
      <TimelineView key={site.id} cameras={cameras} focus={focus} onClearFocus={clearFocus} apiFor={siteApi} layoutStore={layoutStore} storageKey={storageKey} />
    </NavContext.Provider>
  );
}
