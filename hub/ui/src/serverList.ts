/**
 * Customer → Servers as a list (customer/ServersBox.tsx): which admin actions a server offers, its cells' text
 * (cameras, recording, AI, network), problem detection, search and Site filter, grouping by Site, and the
 * "update available" check. Pure, so vitest covers it.
 */
import type { Server } from "./api";
import { fmtGB, fmtMbps, quotaText, serverBandwidth } from "./central";

/**
 * The admin actions a server offers (servers.tsx ServerActions / ServerMenu). Viewers get none (only the console link).
 * A central recording server keeps Rename, Note and Backups: it stays in the Site it was allocated to, its token is the
 * instance's own, and retiring or removing it is done on the Hosts page; a retired one may still be restored.
 * `otherSites`: how many Sites it could move to.
 */
export type ServerActionSet = { rename: boolean; note: boolean; move: boolean; backups: boolean; rotate: boolean; retire: "retire" | "restore" | null; remove: boolean };
export function serverActions(s: Pick<Server, "central" | "retired_at">, admin: boolean, otherSites: number): ServerActionSet {
  const box = admin && !s.central;   // actions for an ordinary server only
  return {
    rename: admin, note: admin, move: box && otherSites > 0, backups: admin, rotate: box,
    retire: admin && (!s.central || s.retired_at) ? (s.retired_at ? "restore" : "retire") : null,
    remove: box,
  };
}
export const anyAction = (a: ServerActionSet) => a.rename || a.note || a.move || a.backups || a.rotate || !!a.retire || a.remove;

type Cams = Pick<Server, "summary">;
/** A camera counts as down when it has no stream or reports a problem (the red chip of the server card). */
export const camDown = (c: { stream_ready: boolean; problems?: string[] }) => !c.stream_ready || !!c.problems?.length;
export function cameraCounts(s: Cams): { up: number; total: number; down: string[] } {
  const cams = s.summary?.cameras ?? [];
  const down = cams.filter(camDown).map((c) => c.name);
  return { up: cams.length - down.length, total: cams.length, down };
}
/** "1 down: Speaker Back Wall", "4 down: Gate, Dock +2" (null when none is down). */
export function downText(down: string[], max = 2): string | null {
  if (!down.length) return null;
  const more = down.length - max;
  return `${down.length} down: ${down.slice(0, max).join(", ")}${more > 0 ? ` +${more}` : ""}`;
}

/** Days the free disk lasts at the cameras' current bitrate (the server card's "~N d"); null when unknown. */
export function daysLeft(s: Cams): number | null {
  const sm = s.summary ?? {};
  if (!sm.disk || !sm.bitrate_mbps) return null;
  const d = Math.round(sm.disk.free_gb / ((sm.bitrate_mbps * 86400) / 8 / 1000));
  return isFinite(d) ? d : null;
}
/** "7.1 TB free · room for ~46 days"; a central instance shows its storage quota ("186 GB of 2.0 TB (9%)"). */
export function recordingText(s: Pick<Server, "summary" | "central">): string {
  if (s.central) return quotaText(s.central.used_gb, s.central.quota_gb);
  const disk = s.summary?.disk;
  if (!disk) return "—";
  const d = daysLeft(s);
  return `${fmtGB(disk.free_gb)} free${d != null ? ` · room for ~${d} day${d === 1 ? "" : "s"}` : ""}`;
}
/** "YOLO ✓ · Qwen ✗ (3 waiting)". The heartbeat summary doesn't say which device YOLO runs on, so it isn't shown. */
export function aiText(s: Cams): string {
  const sm = s.summary ?? {};
  return `YOLO ${sm.yolo_ready ? "✓" : "✗"} · Qwen ${sm.vlm_ready ? "✓" : "✗"}${sm.queues?.synopsis ? ` (${sm.queues.synopsis} waiting)` : ""}`;
}
/** "16.8 Mbit/s in · 4.7 GB this month": what the cameras send the server; null when it reports none. */
export function networkText(s: Cams): string | null {
  const bw = serverBandwidth(s);
  return bw ? `${fmtMbps(bw.mbps)} in${bw.month_gb != null ? ` · ${fmtGB(bw.month_gb)} this month` : ""}` : null;
}

/** Offline, a camera down, or open alerts. A retired server is never a problem (it is out of service on purpose). */
export function hasProblems(s: Pick<Server, "online" | "open_alerts" | "retired_at" | "summary">): boolean {
  if (s.retired_at) return false;
  return !s.online || s.open_alerts > 0 || cameraCounts(s).down.length > 0;
}

/** Version strings compared part by part, numerically where both parts are numbers ("0.10.0" > "0.9.3"). */
export function compareVersions(a: string, b: string): number {
  const pa = a.trim().replace(/^v/i, "").split(/[.+-]/), pb = b.trim().replace(/^v/i, "").split(/[.+-]/);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const x = pa[i] ?? "0", y = pb[i] ?? "0";
    const nx = Number(x), ny = Number(y);
    const c = /^\d+$/.test(x) && /^\d+$/.test(y) ? nx - ny : x.localeCompare(y);
    if (c) return c < 0 ? -1 : 1;
  }
  return 0;
}
/** The highest version among the servers in service (null when none reports one). */
export function latestVersion(servers: Pick<Server, "version" | "retired_at">[]): string | null {
  let best: string | null = null;
  for (const s of servers) if (!s.retired_at && s.version && (best == null || compareVersions(s.version, best) > 0)) best = s.version;
  return best;
}
export const updateAvailable = (s: Pick<Server, "version">, latest: string | null) => !!s.version && !!latest && compareVersions(s.version, latest) < 0;

/** The toolbar: search text, a Site id ("" = all sites, UNASSIGNED = servers without one), only servers with problems. */
export type ServerFilters = { q: string; site: string; problems: boolean };
export const UNASSIGNED = "__unassigned__";
export const NO_FILTERS: ServerFilters = { q: "", site: "", problems: false };

/** Search matches the server's name, note, host, Site name and camera names (case-insensitive, every word). */
export function matchesSearch(s: Pick<Server, "name" | "location" | "hostname" | "location_name" | "summary">, q: string): boolean {
  const words = q.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return true;
  const hay = [s.name, s.location, s.hostname ?? "", s.location_name ?? "", ...(s.summary?.cameras ?? []).map((c) => c.name)].join("\n").toLowerCase();
  return words.every((w) => hay.includes(w));
}
export function filterServers<T extends Server>(servers: T[], f: ServerFilters): T[] {
  return servers.filter((s) => matchesSearch(s, f.q)
    && (!f.site || (f.site === UNASSIGNED ? !s.location_id : s.location_id === f.site))
    && (!f.problems || hasProblems(s)));
}

const byName = (a: { name: string }, b: { name: string }) => a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: "base" });
export type ServerGroup<T> = { id: string; name: string; servers: T[] };
/**
 * One group per Site (by name, its servers by name), then the servers without a Site; retired servers apart, for the
 * collapsed section at the bottom. `siteNames` names a Site the server rows don't (location_name missing).
 */
export function groupBySite<T extends Server>(servers: T[], siteNames: Record<string, string> = {}): { groups: ServerGroup<T>[]; unassigned: T[]; retired: T[] } {
  const bySite = new Map<string, ServerGroup<T>>();
  const unassigned: T[] = [], retired: T[] = [];
  for (const s of servers) {
    if (s.retired_at) { retired.push(s); continue; }
    if (!s.location_id) { unassigned.push(s); continue; }
    let g = bySite.get(s.location_id);
    if (!g) bySite.set(s.location_id, (g = { id: s.location_id, name: s.location_name ?? siteNames[s.location_id] ?? s.location_id, servers: [] }));
    g.servers.push(s);
  }
  const groups = [...bySite.values()].sort(byName);
  for (const g of groups) g.servers.sort(byName);
  return { groups, unassigned: unassigned.sort(byName), retired: retired.sort(byName) };
}

/** Saved toolbar state (localStorage, per viewer); anything malformed falls back to no filters. */
export function parseFilters(raw: string | null): ServerFilters {
  try {
    const v = raw ? JSON.parse(raw) : null;
    if (!v || typeof v !== "object") return NO_FILTERS;
    return { q: typeof v.q === "string" ? v.q : "", site: typeof v.site === "string" ? v.site : "", problems: v.problems === true };
  } catch { return NO_FILTERS; }
}
