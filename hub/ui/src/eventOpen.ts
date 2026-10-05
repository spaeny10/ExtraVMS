/**
 * Pure helpers behind opening a server's event in place on hub pages (HubEventDetail.tsx), as the server UI does:
 * a click on an event card or an event alert shows the clip and synopsis in the event viewer, and only the viewer's
 * own "Open in Timeline" goes to the Site's combined Timeline.
 *
 * Also the Site Live activity feed (SiteLive.tsx), which mirrors the server UI's LiveView: a pool of the Site's recent
 * events kept fresh from the fleet socket, deepened per painted camera, and filtered by the painted regions. Regions
 * are stored per lane key camKey(server, camera) (region.ts store, shared with the Timeline).
 */
import type { NvrEvent } from "@site/api";
import type { FleetEvent } from "@site/dashboard/types";
import type { TimelineTarget } from "@site/nav";
import { camKey, splitKey } from "@site/playback";
import { regionPass } from "@site/region";
import { consoleTimelineHref } from "./nav";
import { siteTimelineHref } from "./timelineLink";

/** An event to open: its server (event ids are per server), the id, and the server's Site when known. */
export type EventRef = { server: string; id: number; location?: string | null };

/**
 * Where the viewer's "Open in Timeline" goes for an event of `server`: the Site's combined Timeline focused on it (a
 * journey keeps all its sightings, keyed by its first camera as SiteTimeline's own links are), else the server's
 * console when the server is in no Site (the combined Timeline needs one).
 */
export function timelineTargetHref(location: string | null | undefined, server: string, t: TimelineTarget): string {
  const journey = !!t.members?.length;
  const cam = journey ? t.members![0].cam : t.camera_id;
  if (location) return siteTimelineHref(location, server, cam, t.id || null, t.id ? null : t.start_ts, journey);
  return consoleTimelineHref(server, t.id ? { cam, event: t.id } : { cam, t: t.start_ts });
}

/** The event an alert is about (event alerts carry the server's event in `detail`); null for server/health alerts. */
export function alertEvent(a: { site_id: string; location_id?: string | null; detail: Record<string, unknown> }): EventRef | null {
  const d = a.detail as { id?: unknown; camera_id?: unknown };
  const id = Number(d.id);
  if (!d.camera_id || !Number.isInteger(id) || id <= 0) return null;
  return { server: a.site_id, id, location: a.location_id ?? null };
}

/** A camera-name lookup for the viewer: names keyed camKey(server, camera), falling back to the camera id. */
export const cameraNameFor = (names: ReadonlyMap<string, string>, server: string) => (cam: string) => names.get(camKey(server, cam)) ?? cam;

// ---------------------------------------------------------------- Site Live activity feed

export const poolKey = (e: Pick<FleetEvent, "site_id" | "id">) => `${e.site_id}/${e.id}`;
const FEED_POOL_MAX = 200;

/** A server's events tagged with their server (the server's own API returns them bare). */
export const tagServer = (events: NvrEvent[], server: string, serverName: string): FleetEvent[] =>
  events.map((e) => ({ ...e, site_id: server, site_name: serverName }));

/**
 * A live update applied to the recent pool, as EventsWidget does: the newest copy replaces the old one, events the
 * verifier threw out (rejected) or a privacy mask hid leave the feed, the rest stays newest first.
 */
export function applyLiveEvent(pool: FleetEvent[], e: FleetEvent): FleetEvent[] {
  const rest = pool.filter((x) => poolKey(x) !== poolKey(e));
  if (e.status === "rejected" || e.status === "masked") return rest;
  return [e, ...rest].sort((a, b) => b.start_ts - a.start_ts).slice(0, FEED_POOL_MAX);
}

export const removeLiveEvent = (pool: FleetEvent[], server: string, id: number): FleetEvent[] =>
  pool.filter((x) => !(x.site_id === server && x.id === id));

/** LiveView's pool: the deeper per-camera history of painted cameras merged under the live recent list (recent wins). */
export function mergePool(scoped: FleetEvent[], recent: FleetEvent[]): FleetEvent[] {
  return [...new Map([...scoped, ...recent].map((e) => [poolKey(e), e])).values()].sort((a, b) => b.start_ts - a.start_ts);
}

/**
 * The painted regions that scope this Site's feed: lane keys of this Site's servers only. The region store is shared
 * by the whole origin, so it also holds regions painted on other Sites and on servers' own consoles (keyed by bare
 * camera id) that must not empty this feed.
 */
export function siteRegionKeys(regionMap: Record<string, Uint8Array>, servers: readonly string[]): string[] {
  const own = new Set(servers);
  return Object.keys(regionMap).filter((k) => { const { server } = splitKey(k); return !!server && own.has(server); }).sort();
}

/** The feed: everything when nothing is painted, else only the painted cameras' events that crossed their region. */
export function regionFeed(pool: FleetEvent[], regionMap: Record<string, Uint8Array>, keys: readonly string[]): FleetEvent[] {
  if (!keys.length) return pool;
  const scoped = new Set(keys);
  return pool.filter((e) => { const k = camKey(e.site_id, e.camera_id); return scoped.has(k) && regionPass(e, regionMap[k]); });
}
