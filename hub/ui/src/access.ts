/** Small pure helpers shared by the Sites pages and Customer admin (kept apart from components so they are unit-tested). */
import type { Access, Me, Org, Server, Site, SocRole } from "./api";

export const isAdmin = (org: Org | undefined, me: Me) => me.user.is_super || org?.role === "admin" || org?.role === "owner";

/**
 * May spend CoverageMap units (look a Site up again, check an address): hub administrators; on the paid plan also the
 * customer's admins and owners with a real membership (a customer listed only through the SOC does not count). The hub
 * enforces the same (api.py _coverage_can_fetch); this only decides whether to offer the button.
 */
export function canCheckCoverage(me: Me, org: Org | undefined): boolean {
  if (!me.coverage?.enabled || !me.coverage.visible) return false;
  if (me.user.is_super) return true;
  if (me.coverage.plan !== "paid" || !org) return false;
  const real = org.member === true || (org.member === undefined && !org.soc);
  return real && (org.role === "admin" || org.role === "owner");
}

/** The user's SOC role. Hub administrators count as supervisors (the hub's soc_level does the same). */
export const socRole = (me: Me): SocRole | null => (me.user.is_super ? "supervisor" : me.user.soc_role ?? null);
export const isSocUser = (me: Me) => socRole(me) !== null;
export const isSocSupervisor = (me: Me) => socRole(me) === "supervisor";

/**
 * Who configures a Site's monitoring, contacts and procedures: the customer's admins (it is their Site and their call
 * list) and SOC supervisors (they set Sites up during onboarding). SOC operators and customer operators may only arm
 * or disarm now.
 */
export const canEditMonitoring = (org: Org | undefined, me: Me) => isAdmin(org, me) || isSocSupervisor(me);

/**
 * Where `/` sends someone: SOC staff with no real customer membership (every customer they see is there only through
 * the SOC) go to their console, supervisors to the supervisor view. Hub administrators and customer members keep Home.
 */
export function socLanding(me: Me): string | null {
  const role = socRole(me);
  if (!role || me.user.is_super) return null;
  // the hub's own verdict when it sends one; otherwise read it off the customer list
  const socOnly = me.user.soc_only ?? !me.orgs.some((o) => o.member === true || (o.member === undefined && !o.soc));
  if (!socOnly) return null;
  return role === "supervisor" ? "/soc/supervisor" : "/soc";
}

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

/**
 * One Site's part of a customer digest, in the digest's own Site grouping order when it has one. Null when the digest
 * predates per-server data (callers then show the whole text). A server moved since the digest still counts for the
 * Site it was in when it was written (location_id), plus any of the Site's current servers.
 */
export function digestPartsFor<P extends { site_id: string; location_id?: string | null }>(
  data: { sites?: P[]; locations?: { id: string | null; servers: string[] }[] } | null | undefined,
  site: { id: string; servers: { id: string }[] },
): P[] | null {
  if (!data?.sites) return null;
  const mine = new Set(site.servers.map((s) => s.id));
  const parts = data.sites.filter((p) => p.location_id === site.id || mine.has(p.site_id));
  const order = data.locations?.find((g) => g.id === site.id)?.servers ?? [];
  const rank = (id: string) => { const i = order.indexOf(id); return i < 0 ? order.length : i; };
  return [...parts].sort((a, b) => rank(a.site_id) - rank(b.site_id));
}
