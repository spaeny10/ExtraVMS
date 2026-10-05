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
import { KIND_LABEL } from "./labels";
import { siteHref } from "./nav";
import { siteTimelineHref } from "./timelineLink";

const clients = new Map<string, SiteApi>();
export const siteApi = (site: string): SiteApi => {
  let c = clients.get(site);
  if (!c) { c = makeApi(`/s/${site}`); clients.set(site, c); }
  return c;
};

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
