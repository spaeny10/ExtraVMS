/**
 * The dashboard's view of the fleet: cameras and servers from the Fleet summaries, per-server API clients under
 * /s/<server>/, events and live updates from the hub's fan-out endpoints, alerts/digest/Ask from the hub. The dashboard's
 * `site` fields (SourceCamera.site, CameraRef.site) are server ids: the wire name predates Sites (= locations).
 */
import { makeApi, type SiteApi } from "@site/api";
import type { DashboardSource, SourceCamera, SourceSite } from "@site/dashboard/source";
import type { CameraGroup } from "@site/dashboard/types";
import { timelineHash } from "@site/nav";
import { api, subscribeFleet, type Fleet, type Org, type Server } from "./api";
import { directEntry } from "./direct";
import { KIND_LABEL } from "./labels";
import { siteHref } from "./nav";
import { siteTimelineHref } from "./timelineLink";

const clients = new Map<string, SiteApi>();
/** The hub-proxied client for a server (/s/<id>/…): every write, and media whenever Direct-on-LAN isn't active. */
export const siteApi = (site: string): SiteApi => {
  let c = clients.get(site);
  if (!c) { c = makeApi(`/s/${site}`); clients.set(site, c); }
  return c;
};

/** The SiteApi members that only build media URLs (or read what playback can do): these may go direct. */
const MEDIA_KEYS = ["media", "frameUrl", "playbackUrl", "whepUrl", "playbackCapabilities"] as const;

/**
 * A client whose media URLs point straight at the server on its LAN and whose REST calls (and so every write: locks,
 * feedback, PTZ, regions) stay on the hub proxy, since the server accepts a direct token only for reads and WHEP.
 * The token is read at call time, so a re-minted token doesn't change the client's identity (a new identity would
 * make every live tile reconnect); a new base URL does.
 */
export function hybridApi(hub: SiteApi, base: string, token: () => string): SiteApi {
  // ?direct= on every URL (a <video>/<img> src can't send a header) and Authorization: Direct on fetches; the
  // handshake's cookie is not relied on: Safari and Chrome block it as a third-party cookie
  const lan = () => { const t = token(); return makeApi(base, { query: { direct: t }, fetchInit: { headers: { Authorization: `Direct ${t}` } } }); };
  const c: SiteApi = { ...hub };
  const m = c as unknown as Record<string, unknown>;
  for (const k of MEDIA_KEYS) m[k] = (...a: unknown[]) => (lan()[k] as (...x: unknown[]) => unknown)(...a);
  return c;
}

const directClients = new Map<string, { base: string; client: SiteApi }>();
/**
 * The client for a server's media (live tiles, playback, frames, snapshots, clips): direct when this browser reaches
 * the server on its LAN (direct.ts), else siteApi(server). Components that call it should also call useDirect /
 * useDirectVersion for the server so they re-render when the route changes.
 */
export function mediaApi(server: string): SiteApi {
  const d = directEntry(server);
  if (d?.state !== "direct" || !d.base || !d.token) return siteApi(server);
  const base = d.base;
  let hit = directClients.get(server);
  if (!hit || hit.base !== base) {
    hit = { base, client: hybridApi(siteApi(server), base, () => directEntry(server)?.token ?? d.token!) };
    directClients.set(server, hit);
  }
  return hit.client;
}
/** Is this server's media going direct right now? */
export const isDirect = (server: string): boolean => directEntry(server)?.state === "direct";

/** The customer's servers (the fleet's flat list). */
export function fleetServers(fleet: Fleet | null, org: Org): Server[] {
  return fleet?.orgs.find((o) => o.org.id === org.id)?.sites ?? [];
}

type SiteOf = (server: string) => string | null | undefined;

/**
 * Where a dashboard event opens: the combined Timeline of the server's Site, so the user stays in the hub with the
 * Site's other cameras beside it. The server's own console is the fallback when its Site isn't known (not in this
 * fleet snapshot yet) or the event names no camera (a briefing line), since a Site Timeline link needs both.
 */
export function eventLink(siteOf: SiteOf, e: { site_id: string; camera_id?: string; id: number }): string {
  const loc = siteOf(e.site_id);
  return loc && e.camera_id ? siteTimelineHref(loc, e.site_id, e.camera_id, e.id) : `/s/${e.site_id}/${timelineHash(e.camera_id ?? "", e.id)}`;
}

/** A server's "open live": its Site's combined Live view when known, else the server's own console. */
export function liveLink(siteOf: SiteOf, server: string): string {
  const loc = siteOf(server);
  return loc ? siteHref(loc, "live") : `/s/${server}/#live`;
}

export function makeHubSource(org: Org, fleet: Fleet | null, groups: CameraGroup[]): DashboardSource {
  const sites = fleetServers(fleet, org);
  const cameras: SourceCamera[] = sites.flatMap((s) => (s.summary?.cameras ?? []).map((c) => ({
    site: s.id, siteName: s.name, id: c.id, name: c.name, streamReady: !!c.stream_ready, online: s.online, ptz: !!c.ptz,
  })));
  const siteRows: SourceSite[] = sites.map((s) => ({
    id: s.id, name: s.name, location: s.location, online: s.online,
    cameras: s.summary?.cameras?.length ?? 0, camerasUp: (s.summary?.cameras ?? []).filter((c) => c.stream_ready).length,
    diskFreeGb: s.summary?.disk?.free_gb ?? null, openAlerts: s.open_alerts, version: s.version,
  }));
  const siteOf = (server: string) => sites.find((s) => s.id === server)?.location_id;
  return {
    cameras: () => cameras,
    sites: () => siteRows,
    groups: () => groups,
    siteApi,
    iceServers: (site) => siteApi(site).turn().then((t) => t.iceServers ?? []),
    events: (p) => api.fleetEvents(org.id, p),
    subscribe: (onMessage) => subscribeFleet(org.id, onMessage),
    eventHref: (e) => eventLink(siteOf, e),
    liveHref: (server) => liveLink(siteOf, server),
    port: 8189,
    extras: {
      alerts: () => api.alerts(org.id, true),
      ack: (id) => api.ack(id),
      digest: () => api.digests(org.id).then((d) => (d[0] ? { day: d[0].day, text: d[0].text, model: d[0].model, created_at: d[0].created_at } : null)),
      askHref: (q) => `/find?q=${encodeURIComponent(q)}`,
      kindLabels: KIND_LABEL,
    },
  };
}
