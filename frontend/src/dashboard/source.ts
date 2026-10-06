/**
 * Where a dashboard gets its cameras, events and extras. The fleet hub builds one over every site the user
 * can see (hub/ui/src/hubSource.ts); the site app will build one over itself. Widgets only ever talk to this.
 */
import type { NvrEvent, SiteApi } from "../api";
import type { CameraGroup, EventsProps, FleetEvents } from "./types";

export { camKey, splitKey } from "../playback";

/** `site`/`siteName` are the SERVER id/name (legacy naming, matches the persisted CameraRef). The optional
 *  `server`/`serverName` say the same explicitly; `siteId`/`locationName` are the physical Site the server is in. */
export type SourceCamera = { site: string; siteName: string; id: string; name: string; streamReady: boolean; online: boolean; ptz?: boolean;
  server?: string; serverName?: string; siteId?: string; locationName?: string };
/** A physical Site and the servers in it, for grouping pickers (Site → Server → cameras). */
export type SourceSiteGroup = { id: string; name: string; servers: string[] };
export type SourceSite = { id: string; name: string; location?: string; online: boolean; camerasUp: number; cameras: number; diskFreeGb?: number | null; openAlerts: number; version?: string | null;
  /** Mbit/s the cameras send the server (5-min average) and GB this month: shown when known */
  bandwidth?: { mbps: number; month_gb: number } | null };
export type SourceAlert = { id: number; site_id: string; site_name: string; kind: string; opened_at: number; acked_by: string | null; detail: Record<string, unknown> };
export type SourceDigest = { day: string; text: string; model: string | null; created_at: number } | null;
export type FleetMessage =
  | { type: "event"; event: NvrEvent; site_id: string; site_name: string }
  | { type: "event_removed"; id: number; site_id: string; site_name: string }
  | { type: "site_online" | "site_offline"; site_id: string; site_name: string };

export interface DashboardSource {
  /** every camera the viewer may see, across sites */
  cameras(): SourceCamera[];
  sites(): SourceSite[];
  groups(): CameraGroup[];
  /** physical Sites with their servers, when the source knows them (the hub); absent = no grouping */
  siteGroups?(): SourceSiteGroup[];
  /** API client for one site (URLs under that site's prefix) */
  siteApi(site: string): SiteApi;
  iceServers(site: string): Promise<RTCIceServer[]>;
  events(p: EventsProps): Promise<FleetEvents>;
  subscribe(onMessage: (m: FleetMessage) => void): () => void;
  eventHref(e: { site_id: string; camera_id?: string; id: number }): string;
  /** in-app opener (the site's own Home); when absent widgets navigate to eventHref */
  openEvent?: (e: { site_id: string; camera_id?: string; id: number }) => void;
  liveHref(site: string): string;
  /** the WebRTC port the site UI was told about (only used as an effect key by the players) */
  port: number;
  extras?: {
    alerts?: () => Promise<SourceAlert[]>;
    ack?: (id: number) => Promise<unknown>;
    digest?: () => Promise<SourceDigest>;
    askHref?: (q: string) => string;
    /** in-app Ask (the site's own Home); preferred over askHref */
    ask?: (q: string) => void;
    /** the viewer may generate briefings / change their schedule (site operators; not hub viewers) */
    briefingEditable?: boolean;
    cameraName?: (id: string) => string;
    kindLabels?: Record<string, string>;
  };
}

/** Does an event from `siteId`/`cameraId` belong in a feed with these props? */
export function eventInScope(p: EventsProps, groups: CameraGroup[], siteId: string, cameraId: string, cls: string): boolean {
  if (p.classes?.length && !p.classes.includes(cls as "person" | "vehicle")) return false;
  const anyFilter = !!(p.group || p.cameras?.length || p.sites?.length);
  if (!anyFilter) return true;
  if (p.group) {
    const g = groups.find((x) => x.id === p.group);
    if (g?.members.some((m) => m.site_id === siteId && m.camera_id === cameraId)) return true;
  }
  if (p.cameras?.some((c) => c.site === siteId && c.camera === cameraId)) return true;
  if (p.sites?.includes(siteId)) return true;
  return false;
}
