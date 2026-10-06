/**
 * Central recording (hub/hub/hosts.py), the pure parts: host capacity bars, instance phases, the Peplink settings sheet
 * and the bandwidth / storage numbers shown on Site → Servers and Customer → Servers. Kept apart from the components
 * so they are unit-tested (central.test.ts).
 */
import type { CentralInstance, HostCapacity, Server } from "./api";

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
    const load = c.load ?? 0;
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
  if (!f.quota_gb.trim() || !Number.isInteger(q) || q < 1) return "Enter the storage quota in GB";
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
