/** Small pure helpers shared by the Sites pages and Customer admin (kept apart from components so they are unit-tested). */
import type { Access, Me, Org, Server, Site } from "./api";

export const isAdmin = (org: Org | undefined, me: Me) => me.user.is_super || org?.role === "admin" || org?.role === "owner";

/**
 * One checkbox in a member's Site list changed. Ticking "All sites" keeps the list (so unticking it later restores the
 * member's previous Sites instead of leaving them with nothing); a Site toggle only makes sense while all_sites is off.
 */
export function toggleAccess(a: Access, change: { all: boolean } | { location: string; on: boolean }): Access {
  if ("all" in change) return { ...a, all_sites: change.all };
  const ids = a.location_ids.filter((x) => x !== change.location);
  return { ...a, location_ids: change.on ? [...ids, change.location] : ids };
}

/** "All sites", "No sites" or the names (unknown ids shown as-is, e.g. a Site deleted meanwhile). */
export function accessLabel(a: Access, sites: Pick<Site, "id" | "name">[]): string {
  if (a.all_sites) return "All sites";
  if (!a.location_ids.length) return "No sites";
  return a.location_ids.map((id) => sites.find((s) => s.id === id)?.name ?? id).join(", ");
}

/** Newest event per Site, from a fleet event list whose `site_id` is the server id. */
export function lastEventBySite(events: { site_id: string; start_ts: number }[], servers: Pick<Server, "id" | "location_id">[]): Record<string, number> {
  const siteOf = new Map(servers.map((s) => [s.id, s.location_id ?? null]));
  const out: Record<string, number> = {};
  for (const e of events) {
    const loc = siteOf.get(e.site_id);
    if (loc && (out[loc] ?? 0) < e.start_ts) out[loc] = e.start_ts;
  }
  return out;
}

/** "2/3 online" style counts; "—" when there is nothing to count. */
export const ofTotal = (n: number, total: number, word: string) => (total ? `${n}/${total} ${word}` : "—");

/** Every server a Site page can act on (the rollup leaves retired ones out unless asked for). */
export const siteServers = (s: Site | null | undefined): Server[] => s?.servers ?? [];
