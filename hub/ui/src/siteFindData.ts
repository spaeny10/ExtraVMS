/**
 * Pure helpers behind the Site page's Find tab (SiteFind.tsx): the server UI's FindView over every server of the Site.
 * Cameras are keyed by lane key camKey(server, camera) as on the Site Timeline, named "Server · Camera" when the Site
 * has more than one server (whereLabel); events are keyed "server/id" (ids are per server).
 */
import type { Camera as ServerCam, EventQuery } from "@site/api";
import type { BrowseQuery } from "@site/findSource";
import { camKey, splitKey } from "@site/playback";
import { whereLabel } from "./labels";

/** An event of the hub's fan-out (tagged with its server) as a key unique on the page. */
export const hubEventKey = (e: { site_id?: string; id: number }): string => `${e.site_id ?? ""}/${e.id}`;

/** Newest first across servers: start time, then id. */
export const newerAcross = (a: { start_ts: number; id: number }, b: { start_ts: number; id: number }): number =>
  b.start_ts - a.start_ts || b.id - a.id;

/**
 * FindView's query as the hub's /find/events|search parameters: the camera's lane key becomes server:camera, flags a
 * comma list, attention true or absent; plus the page size and the cursor from the previous page.
 */
export function hubFindParams(q: BrowseQuery | EventQuery, cursor: string | null, limit: number): Record<string, string | number | boolean | undefined> {
  const { camera, flags, attention, offset: _o, limit: _l, ...rest } = q as BrowseQuery;
  void _o; void _l;
  let cam: string | undefined;
  if (camera) {
    const { server, id } = splitKey(camera);
    cam = server ? `${server}:${id}` : id;
  }
  return { ...rest, camera: cam, flags: flags?.length ? flags.join(",") : undefined, attention: attention ? true : undefined, limit, cursor: cursor ?? undefined };
}

/**
 * The camera picker: every camera of the Site's servers (in Site order), id = lane key, name "Server · Camera" (just
 * the camera when the Site has one server). Zones come along so FindView can offer the named places.
 */
export function siteFindCameras(servers: { id: string; name: string }[], byServer: Record<string, ServerCam[] | undefined>): ServerCam[] {
  return servers.flatMap((s) => (byServer[s.id] ?? []).map((c) => ({
    ...c, id: camKey(s.id, c.id),
    name: whereLabel({ server: s.name, camera: c.name || c.id }, { showSite: false, serverCount: servers.length }),
  })));
}
