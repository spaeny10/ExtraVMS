/**
 * The site's own view of itself for the Home dashboard: one "site" (id "local"), its cameras, its events
 * over the page's api, live updates from the site socket. Alerts and the org digest are hub features.
 */
import { api, type Camera, type HomeData } from "../api";
import type { DashboardSource, SourceCamera, SourceSite } from "./source";
import type { CameraRef, EventsProps } from "./types";

export const LOCAL = "local";

export function makeLocalSource(cameras: Camera[], home: HomeData | null, port: number, opts: {
  siteName?: string; openEvent: (e: { site_id: string; camera_id?: string; id: number }) => void; ask: (q: string) => void;
}): DashboardSource {
  const name = opts.siteName ?? "This site";
  const byId = new Map(cameras.map((c) => [c.id, c]));
  const cams: SourceCamera[] = cameras.filter((c) => c.enabled).map((c) => ({
    site: LOCAL, siteName: name, id: c.id, name: c.name, streamReady: !!c.status?.stream_ready, online: true, ptz: !!c.status?.ptz?.available,
  }));
  const site: SourceSite = {
    id: LOCAL, name, online: true, cameras: cams.length, camerasUp: cams.filter((c) => c.streamReady).length,
    diskFreeGb: home?.disk.free_gb ?? null, openAlerts: home?.health_alerts?.length ?? 0,
  };
  const wanted = (p: EventsProps): CameraRef[] | null => (p.cameras?.length ? p.cameras.filter((c) => c.site === LOCAL) : null);
  return {
    cameras: () => cams,
    sites: () => [site],
    groups: () => [],
    siteApi: () => api,
    iceServers: () => api.turn().then((t) => t.iceServers ?? []).catch(() => []),
    events: async (p) => {
      const want = wanted(p);
      const label = p.classes?.length === 1 ? p.classes[0] : undefined;
      const camera = want?.length === 1 ? want[0].camera : undefined;
      const rows = await api.events({ limit: Math.min(p.limit ?? 20, 100), status: "verified,open,pending", label, camera });
      const keep = rows.filter((e) => (!want || want.some((c) => c.camera === e.camera_id)) && (!p.classes?.length || p.classes.includes(e.camera_class as "person" | "vehicle")));
      return { events: keep.map((e) => ({ ...e, site_id: LOCAL, site_name: name })), offline: [], errors: [] };
    },
    subscribe: (onMessage) => api.subscribe((e) => onMessage({ type: "event", event: e, site_id: LOCAL, site_name: name }),
      (m) => { if (m.type === "event_removed") onMessage({ type: "event_removed", id: m.id as number, site_id: LOCAL, site_name: name }); }),
    eventHref: () => "#timeline",
    openEvent: (e) => opts.openEvent(e),
    liveHref: () => "#live",
    port,
    extras: {
      ask: opts.ask,
      briefingEditable: true,
      cameraName: (id) => byId.get(id)?.name ?? id,
    },
  };
}
