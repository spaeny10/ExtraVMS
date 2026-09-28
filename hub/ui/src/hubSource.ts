/**
 * The dashboard's view of the fleet: cameras and sites from the Fleet summaries, per-site API clients under
 * /s/<site>/, events and live updates from the hub's fan-out endpoints, alerts/digest/Ask from the hub.
 */
import { makeApi, type SiteApi } from "@site/api";
import type { DashboardSource, SourceCamera, SourceSite } from "@site/dashboard/source";
import type { CameraGroup } from "@site/dashboard/types";
import { timelineHash } from "@site/nav";
import { api, subscribeFleet, type Fleet, type Org, type Site } from "./api";
import { KIND_LABEL } from "./labels";

const clients = new Map<string, SiteApi>();
export const siteApi = (site: string): SiteApi => {
  let c = clients.get(site);
  if (!c) { c = makeApi(`/s/${site}`); clients.set(site, c); }
  return c;
};

export function fleetSites(fleet: Fleet | null, org: Org): Site[] {
  return fleet?.orgs.find((o) => o.org.id === org.id)?.sites ?? [];
}

export function makeHubSource(org: Org, fleet: Fleet | null, groups: CameraGroup[]): DashboardSource {
  const sites = fleetSites(fleet, org);
  const cameras: SourceCamera[] = sites.flatMap((s) => (s.summary?.cameras ?? []).map((c) => ({
    site: s.id, siteName: s.name, id: c.id, name: c.name, streamReady: !!c.stream_ready, online: s.online, ptz: !!c.ptz,
  })));
  const siteRows: SourceSite[] = sites.map((s) => ({
    id: s.id, name: s.name, location: s.location, online: s.online,
    cameras: s.summary?.cameras?.length ?? 0, camerasUp: (s.summary?.cameras ?? []).filter((c) => c.stream_ready).length,
    diskFreeGb: s.summary?.disk?.free_gb ?? null, openAlerts: s.open_alerts, version: s.version,
  }));
  return {
    cameras: () => cameras,
    sites: () => siteRows,
    groups: () => groups,
    siteApi,
    iceServers: (site) => siteApi(site).turn().then((t) => t.iceServers ?? []),
    events: (p) => api.fleetEvents(org.id, p),
    subscribe: (onMessage) => subscribeFleet(org.id, onMessage),
    eventHref: (e) => `/s/${e.site_id}/${timelineHash(e.camera_id ?? "", e.id)}`,
    liveHref: (site) => `/s/${site}/#live`,
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
