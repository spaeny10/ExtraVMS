/**
 * The Site health widget's rows: one per physical Site (the hub's Customer › Site › Server), its servers rolled up.
 * A server that belongs to no Site (the NVR's own Home, or a hub server not placed yet) is a row of its own.
 * Rows with a problem (a server offline, a camera down, an open alert) come first, then by name. Pure, so it is tested
 * on its own (health.test.ts).
 */
import type { SourceSite } from "./source";

export type HealthRow = {
  key: string;
  name: string;
  /** the Site's servers (one for a server row) */
  servers: SourceSite[];
  online: number;
  cameras: number;
  camerasUp: number;
  openAlerts: number;
  problem: boolean;
  /** the servers listed under a several-server Site that has a problem: the ones with one, so it says which box */
  problemServers: SourceSite[];
};

export const serverProblem = (s: SourceSite) => !s.online || s.openAlerts > 0 || s.camerasUp < s.cameras;

export function healthRows(sites: SourceSite[]): HealthRow[] {
  const groups = new Map<string, { name: string; servers: SourceSite[] }>();
  for (const s of sites) {
    const key = s.locationId ? `loc:${s.locationId}` : `srv:${s.id}`;
    const g = groups.get(key) ?? { name: s.locationId ? s.locationName || s.name : s.name, servers: [] };
    g.servers.push(s);
    groups.set(key, g);
  }
  const rows: HealthRow[] = [...groups].map(([key, g]) => {
    const up = g.servers.filter((s) => s.online);
    const problemServers = g.servers.filter(serverProblem);
    return {
      key, name: g.name, servers: g.servers, online: up.length,
      cameras: g.servers.reduce((n, s) => n + s.cameras, 0),
      // cameras of an offline server aren't up, whatever its last report said
      camerasUp: up.reduce((n, s) => n + s.camerasUp, 0),
      openAlerts: g.servers.reduce((n, s) => n + s.openAlerts, 0),
      problem: problemServers.length > 0,
      problemServers: g.servers.length > 1 ? problemServers : [],
    };
  });
  return rows.sort((a, b) => Number(b.problem) - Number(a.problem) || a.name.localeCompare(b.name));
}
