/**
 * Central recording (hub/hub/hosts.py), the pure parts: host capacity bars, instance phases, the Peplink settings sheet
 * and the bandwidth / storage numbers shown on Site → Servers and Customer → Servers. Kept apart from the components
 * so they are unit-tested (central.test.ts).
 */
import type { CentralInstance, HostCapacity, HubSitesOrg, Peplink, Server } from "./api";

/** provisioning (the host is creating it) → waiting_enroll (created, not dialed in yet) → running. */
export const PHASE_LABEL: Record<string, string> = {
  provisioning: "Provisioning on the host…", waiting_enroll: "Waiting for the instance to enroll…", running: "Running",
  failed: "Failed", deleting: "Removing…", deleted: "Removed",
};
export const PHASE_STEPS = ["provisioning", "waiting_enroll", "running"] as const;
/** Index of the phase in PHASE_STEPS (-1 for failed/deleting/deleted): the progress strip lights steps up to it. */
export const phaseStep = (phase: string) => (PHASE_STEPS as readonly string[]).indexOf(phase);
/** Still changing: the Site page polls while this is true. */
export const settling = (i: Pick<CentralInstance, "phase">) => i.phase === "provisioning" || i.phase === "waiting_enroll" || i.phase === "deleting";

/** VPN mode's default camera LAN for a site number (the hub assigns the number; this is only the form's preview). */
export const defaultSubnet = (n: number) => `10.20.${n}.0/24`;

/** 0..100, clamped; null when the total is unknown. */
export function pct(used: number | null | undefined, total: number | null | undefined): number | null {
  if (used == null || !total || total <= 0) return null;
  return Math.max(0, Math.min(100, Math.round((used / total) * 100)));
}

export type Bar = { label: string; used: number; total: number; pct: number; text: string };

/** CPU (load per core), RAM, each GPU's memory and each disk, as bars for the Hosts page. */
export function capacityBars(c: HostCapacity | null | undefined): Bar[] {
  if (!c) return [];
  const out: Bar[] = [];
  if (c.cpus) {
    // the host agent sends the 1, 5 and 15 minute load averages; an older shape was one number
    const load = (Array.isArray(c.load) ? c.load[0] : c.load) ?? 0;
    out.push({ label: "CPU", used: load, total: c.cpus, pct: pct(load, c.cpus) ?? 0, text: `load ${load.toFixed(1)} on ${c.cpus} cores` });
  }
  if (c.ram_gb?.total) {
    const used = c.ram_gb.total - (c.ram_gb.free ?? 0);
    out.push({ label: "RAM", used, total: c.ram_gb.total, pct: pct(used, c.ram_gb.total) ?? 0, text: `${fmtGB(used)} of ${fmtGB(c.ram_gb.total)}` });
  }
  for (const g of c.gpus ?? []) {
    const total = g.mem_total_mb ?? 0;
    const used = g.mem_used_mb ?? 0;
    out.push({ label: `GPU ${g.index} · ${shortGpu(g.name)}`, used, total, pct: pct(used, total) ?? 0,
      text: `${(used / 1024).toFixed(1)} of ${(total / 1024).toFixed(0)} GB${g.util != null ? ` · ${Math.round(g.util)}% busy` : ""}` });
  }
  for (const d of c.disks ?? []) {
    const used = (d.total_gb ?? 0) - (d.free_gb ?? 0);
    out.push({ label: `Disk ${d.path}`, used, total: d.total_gb ?? 0, pct: pct(used, d.total_gb) ?? 0, text: `${fmtGB(d.free_gb ?? 0)} free of ${fmtGB(d.total_gb ?? 0)}` });
  }
  return out;
}

/** "NVIDIA A10" → "A10"; anything else as-is. */
export const shortGpu = (name: string | null | undefined) => (name ?? "GPU").replace(/^NVIDIA\s+/i, "");

/** 812 → "812 GB", 4000 → "4.0 TB", 12.34 → "12.3 GB". */
export function fmtGB(gb: number | null | undefined): string {
  if (gb == null || !isFinite(gb)) return "—";
  if (Math.abs(gb) >= 1000) return `${(gb / 1000).toFixed(1)} TB`;
  return `${gb >= 100 ? Math.round(gb) : Math.round(gb * 10) / 10} GB`;
}

export const fmtMbps = (m: number | null | undefined) => (m == null || !isFinite(m) ? "—" : `${m >= 100 ? Math.round(m) : m.toFixed(1)} Mbit/s`);

/** A server's own upload numbers (heartbeat summary.bandwidth), or null when it reports none. */
export function serverBandwidth(s: Pick<Server, "summary">) {
  const b = s.summary?.bandwidth;
  return b && typeof b.mbps === "number" ? b : null;
}

/** The Site total of its servers' summary.bandwidth (servers that report none are left out); null when none report. */
export function siteBandwidth(servers: Pick<Server, "summary" | "retired_at">[]): { mbps: number; today_gb: number; month_gb: number; servers: number } | null {
  let mbps = 0, today = 0, month = 0, n = 0;
  for (const s of servers) {
    if (s.retired_at) continue;
    const b = serverBandwidth(s);
    if (!b) continue;
    n++;
    mbps += b.mbps ?? 0; today += b.today_gb ?? 0; month += b.month_gb ?? 0;
  }
  return n ? { mbps, today_gb: today, month_gb: month, servers: n } : null;
}

/** "Upload from site: 18.4 Mbit/s · 412 GB this month" */
export function uploadLine(b: { mbps: number; month_gb: number } | null): string | null {
  return b ? `Upload from site: ${fmtMbps(b.mbps)} · ${fmtGB(b.month_gb)} this month` : null;
}

/** "812 GB of 4.0 TB (20%)" for a central instance's storage quota. */
export function quotaText(used: number | null | undefined, quota: number): string {
  const p = pct(used, quota);
  return used == null ? `${fmtGB(quota)} quota` : `${fmtGB(used)} of ${fmtGB(quota)}${p != null ? ` (${p}%)` : ""}`;
}

/** Port-forward mode: camera k (1-based) is reached at the Site's public IP on RTSP base+k and ONVIF base+k. */
export type ForwardRow = { camera: number; name: string | null; rtsp: number; onvif: number };
export function forwardRows(count: number, rtspBase = 5540, onvifBase = 8080, names: (string | null)[] = []): ForwardRow[] {
  const n = Math.max(0, Math.min(50, Math.floor(count)));
  return Array.from({ length: n }, (_, i) => ({ camera: i + 1, name: names[i] ?? null, rtsp: rtspBase + i + 1, onvif: onvifBase + i + 1 }));
}

/** How many rows the forward table shows: the instance's cameras, at least the typical five. */
export const forwardCount = (cameras: number) => Math.max(5, cameras);

/** The first usable address of a subnet ("10.20.7.0/24" → "10.20.7.1"): the BR1's LAN address in VPN mode. */
export function lanGateway(subnet: string | null | undefined): string | null {
  const m = /^(\d+)\.(\d+)\.(\d+)\.(\d+)\/(\d+)$/.exec((subnet ?? "").trim());
  if (!m) return null;
  return `${m[1]}.${m[2]}.${m[3]}.${Number(m[4]) + 1}`;
}

/** The provisioning form's checks, so the button only enables when the hub would accept it. */
export function centralFormError(f: { mode: "vpn" | "forward"; public_ip: string; subnet: string; quota_gb: string }): string | null {
  const q = Number(f.quota_gb);
  if (!f.quota_gb.trim() || !Number.isInteger(q) || q < 10) return "Enter the storage quota in GB (at least 10)";
  if (f.mode === "forward" && !f.public_ip.trim()) return "Port forwarding needs the site's public IP";
  if (f.mode === "vpn" && f.subnet.trim() && !/^10\.\d{1,3}\.\d{1,3}\.\d{1,3}\/\d{1,2}$|^192\.168\.\d{1,3}\.\d{1,3}\/\d{1,2}$|^172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\/\d{1,2}$/.test(f.subnet.trim()))
    return "The camera subnet must be a private network like 10.20.7.0/24";
  return null;
}

/** The site number the hub will most likely assign next (first free 1..250), to prefill the subnet; null when full. */
export function nextSiteNumber(used: (number | null | undefined)[]): number | null {
  const taken = new Set(used.filter((n): n is number => typeof n === "number"));
  for (let n = 1; n <= 250; n++) if (!taken.has(n)) return n;
  return null;
}

// ---- Camera addresses: the instance's camera allow-list on its host (hub hosts.check_camera_network decides; these
// checks mirror it so the editor can flag a row before saving)
export type CameraKind = "subnets" | "public_ips" | "hosts";
export const CAMERA_KINDS: CameraKind[] = ["subnets", "public_ips", "hosts"];
export const MAX_CAMERA_ENTRIES = 32;
export type CameraLists = Record<CameraKind, string[]>;

/** "192.168.1.7" → its 32-bit value; null unless four 0-255 decimal parts. */
export function parseIPv4(s: string): number | null {
  const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(s.trim());
  if (!m) return null;
  const p = m.slice(1).map(Number);
  if (p.some((x) => x > 255)) return null;
  return ((p[0] << 24) >>> 0) + (p[1] << 16) + (p[2] << 8) + p[3];
}

const net = (cidr: string): [number, number] => { const [a, n] = cidr.split("/"); return [parseIPv4(a)!, Number(n)]; };
const mask = (bits: number) => (bits === 0 ? 0 : (0xffffffff << (32 - bits)) >>> 0);
const inNet = (ip: number, [base, bits]: [number, number]) => ((ip & mask(bits)) >>> 0) === ((base & mask(bits)) >>> 0);
/** Two networks overlap when one contains the other's base at the shorter prefix. */
const overlaps = (a: [number, number], b: [number, number]) => { const bits = Math.min(a[1], b[1]); return ((a[0] & mask(bits)) >>> 0) === ((b[0] & mask(bits)) >>> 0); };
const PRIVATE = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10"].map(net);
const HOST_NETS: [[number, number], string][] = [[net("10.200.0.0/16"), "the host's instance pool 10.200.0.0/16"], [net("10.201.0.0/24"), "the host's AI network 10.201.0.0/24"]];

/** cam1.example.net: two or more labels of letters, digits and '-', the last not all digits. */
export function isHostName(s: string): boolean {
  const name = s.trim().toLowerCase().replace(/\.$/, "");
  const labels = name.split(".");
  if (name.length < 1 || name.length > 253 || labels.length < 2 || /^\d+$/.test(labels[labels.length - 1]) || name.endsWith(".localhost")) return false;
  return labels.every((l) => /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/.test(l));
}

/** What is wrong with one row of the editor (null = fine, or empty: empty rows are dropped on save). */
export function cameraEntryError(kind: CameraKind, value: string): string | null {
  const v = value.trim();
  if (!v) return null;
  if (kind === "subnets") {
    const m = /^([\d.]+)\/(\d{1,2})$/.exec(v);
    const ip = m ? parseIPv4(m[1]) : null;
    if (!m || ip == null || Number(m[2]) > 32) return "A subnet like 192.168.105.0/24";
    const n: [number, number] = [ip, Number(m[2])];
    if (n[1] < 16) return "Broader than a /16";
    if (!PRIVATE.some((p) => n[1] >= p[1] && inNet(n[0], p))) return "Must be a private network (10.x, 172.16-31.x, 192.168.x or 100.64-127.x)";
    const bad = HOST_NETS.find(([h]) => overlaps(n, h));
    return bad ? `Overlaps ${bad[1]}` : null;
  }
  if (kind === "public_ips") {
    const ip = parseIPv4(v);
    if (ip == null) return isHostName(v) ? "A DNS name: put it under host names" : "An IPv4 address like 203.0.113.7";
    const first = ip >>> 24;
    if (ip === 0 || first === 127 || first >= 224 || inNet(ip, net("169.254.0.0/16"))) return "Not a usable camera address";
    const bad = HOST_NETS.find(([h]) => inNet(ip, h));
    return bad ? `Inside ${bad[1]}` : null;
  }
  if (parseIPv4(v) != null) return "An IP address: put it under public IPs";
  return isHostName(v) ? null : "A DNS name like cam1.example.net";
}

/** Trimmed, empty rows dropped, host names lowercased, duplicates dropped: what Save sends. */
export function cleanCameraNetwork(l: CameraLists): CameraLists {
  const uniq = (xs: string[]) => [...new Set(xs)];
  return {
    subnets: uniq(l.subnets.map((s) => s.trim()).filter(Boolean)),
    public_ips: uniq(l.public_ips.map((s) => s.trim()).filter(Boolean)),
    hosts: uniq(l.hosts.map((s) => s.trim().toLowerCase().replace(/\.$/, "")).filter(Boolean)),
  };
}

/** The first problem in the whole list (null = Save may go ahead). */
export function cameraNetworkError(l: CameraLists): string | null {
  for (const k of CAMERA_KINDS) for (const v of l[k]) { const e = cameraEntryError(k, v); if (e) return `${v.trim()}: ${e}`; }
  const c = cleanCameraNetwork(l);
  const n = c.subnets.length + c.public_ips.length + c.hosts.length;
  return n > MAX_CAMERA_ENTRIES ? `${n} addresses: at most ${MAX_CAMERA_ENTRIES}` : null;
}

/** The instance's camera network; from subnet / public_ip for an answer without one (an older hub). */
export function cameraNetworkOf(ci: Pick<CentralInstance, "mode" | "subnet" | "public_ip" | "camera_network">): CameraLists {
  const c = ci.camera_network;
  if (c) return { subnets: [...(c.subnets ?? [])], public_ips: [...(c.public_ips ?? [])], hosts: [...(c.hosts ?? [])] };
  if (ci.mode === "vpn") return { subnets: ci.subnet ? [ci.subnet] : [], public_ips: [], hosts: [] };
  const p = ci.public_ip ?? "";
  return { subnets: [], public_ips: p && parseIPv4(p) != null ? [p] : [], hosts: p && parseIPv4(p) == null ? [p] : [] };
}

/** "Cameras reachable at: 192.168.105.0/24 · 203.0.113.7 · cam1.example.net" (or "none"). */
export function cameraNetworkSummary(l: CameraLists): string {
  const all = [...l.subnets, ...l.public_ips, ...l.hosts];
  return `Cameras reachable at: ${all.length ? all.join(" · ") : "none"}`;
}

/** "cam1.example.net → 203.0.113.21", one per DNS name ("not resolved yet" when the host has no address for it). */
export function resolvedLines(hosts: string[], resolved: Record<string, string[] | null> | null | undefined): string[] {
  return hosts.map((h) => { const ips = resolved?.[h] ?? []; return `${h} → ${ips.length ? ips.join(", ") : "not resolved yet"}`; });
}

/** The routers whose port forwards the instance uses (the port-forward table applies to each). */
export function forwardTargets(p: Pick<Peplink, "forward_addresses" | "public_ip">): string[] {
  return p.forward_addresses ?? (p.public_ip ? [p.public_ip] : []);
}

// ---- Camera limit and the Hosts page's Allocate form

export const MAX_CAMERA_LIMIT = 500;

/** The limit field: blank = no limit; else a whole number 1-500. */
export function parseCameraLimit(s: string): { value: number | null; error: null } | { value: null; error: string } {
  const v = s.trim();
  if (!v) return { value: null, error: null };
  const n = Number(v);
  if (!/^\d+$/.test(v) || !Number.isInteger(n) || n < 1 || n > MAX_CAMERA_LIMIT) return { value: null, error: `The camera limit is a number from 1 to ${MAX_CAMERA_LIMIT}, or blank for none` };
  return { value: n, error: null };
}

/** "3 of 5", or "3 (no limit)". */
export function camerasText(count: number | null | undefined, limit: number | null | undefined): string {
  const n = count ?? 0;
  return limit == null ? `${n} (no limit)` : `${n} of ${limit}`;
}

/** More cameras than the limit allows (it was lowered): they keep recording, new ones are refused. */
export const overLimit = (count: number | null | undefined, limit: number | null | undefined) => limit != null && (count ?? 0) > limit;

/** The customer's Sites that have no central instance yet (removed ones don't count), by name. */
export function sitesWithoutCentral(orgs: HubSitesOrg[], orgId: string, instances: Pick<CentralInstance, "location_id" | "state">[]): { id: string; name: string }[] {
  const taken = new Set(instances.filter((c) => c.state !== "deleted").map((c) => c.location_id));
  const org = orgs.find((o) => o.org.id === orgId);
  return (org?.locations ?? []).filter((l) => !taken.has(l.id)).map((l) => ({ id: l.id, name: l.name })).sort((a, b) => a.name.localeCompare(b.name));
}

export type AllocateFields = { org: string; site: string; mode: "vpn" | "forward"; subnet: string; public_ip: string; quota_gb: string; camera_limit: string };

/** The Allocate form's checks (the hub decides; this keeps the button off until it would accept). roomGb: the host's room. */
export function allocateFormError(f: AllocateFields, roomGb: number | null | undefined): string | null {
  if (!f.org) return "Choose the customer";
  if (!f.site) return "Choose the Site";
  const base = centralFormError(f);
  if (base) return base;
  if (f.mode === "forward" && parseIPv4(f.public_ip) == null && !isHostName(f.public_ip)) return "The router's public address is an IP address or a DNS name";
  const lim = parseCameraLimit(f.camera_limit);
  if (lim.error) return lim.error;
  if (roomGb != null && Number(f.quota_gb) > roomGb) return `The host has room for ${fmtGB(Math.max(0, roomGb))}`;
  return null;
}

/** What the firewall allows, split for display: the Site networks, and the cameras' own addresses not already among them. */
export function addressParts(ci: Pick<CentralInstance, "mode" | "subnet" | "public_ip" | "camera_network">): { site: string[]; cameras: string[] } {
  const site = cameraNetworkOf(ci);
  const listed = new Set([...site.subnets, ...site.public_ips, ...site.hosts]);
  const auto = ci.camera_network?.auto;
  return { site: [...site.subnets, ...site.public_ips, ...site.hosts], cameras: [...(auto?.public_ips ?? []), ...(auto?.hosts ?? [])].filter((a) => !listed.has(a)) };
}
