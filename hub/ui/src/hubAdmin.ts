/** Pure helpers for the hub administrator views (the "All customers" Sites page, the Customer picker's extra option). */
import type { HubSitesOrg, Server, Site } from "./api";

/** The Customer picker's value for "All customers"; never a real org id (those start with "o_"). */
export const ALL_CUSTOMERS = "*all";

export type CustomerGroup = {
  org: { id: string; name: string };
  sites: Site[];
  unassigned: Server[];
  servers_online: number;
  servers_total: number;
  open_alerts: number;
};

/**
 * /api/hub/sites rows as the page shows them: customers by name (case-insensitive, like people read a list), each
 * customer's Sites by name, plus the counts for its group header. `q` keeps Sites whose name or address matches, or
 * every Site of a customer whose name matches; a customer left with nothing to show is dropped while filtering, but
 * kept (as "No sites yet") when not, so a new customer is visible to the admin who just created it.
 */
export function groupByCustomer(rows: HubSitesOrg[], q = ""): CustomerGroup[] {
  const needle = q.trim().toLocaleLowerCase();
  const byName = (a: { name: string }, b: { name: string }) => a.name.localeCompare(b.name, undefined, { sensitivity: "base" });
  const out: CustomerGroup[] = [];
  for (const r of [...rows].sort((a, b) => byName(a.org, b.org))) {
    const orgHit = !needle || r.org.name.toLocaleLowerCase().includes(needle);
    const hit = (s: { name: string; address?: string; location?: string }) =>
      orgHit || s.name.toLocaleLowerCase().includes(needle) || (s.address ?? s.location ?? "").toLocaleLowerCase().includes(needle);
    const sites = r.locations.filter(hit).sort(byName);
    const unassigned = (r.unassigned ?? []).filter(hit);
    if (needle && !sites.length && !unassigned.length) continue;
    const live = [...sites.flatMap((s) => s.servers), ...unassigned].filter((v) => !v.retired_at);
    out.push({
      org: r.org, sites, unassigned,
      servers_online: live.filter((v) => v.online).length, servers_total: live.length,
      open_alerts: sites.reduce((n, s) => n + s.open_alerts, 0) + unassigned.reduce((n, v) => n + (v.retired_at ? 0 : v.open_alerts), 0),
    });
  }
  return out;
}
