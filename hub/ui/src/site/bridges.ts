/**
 * Axiom Bridge (hub bridges.py), the pure parts of Site › Servers › Axiom Bridge (BridgesCard.tsx): the install command,
 * the settings sheet (the relay address and ports the instance's camera uses, what Apply to camera writes), status lines
 * and the map form's checks. Kept apart from the component so they are unit-tested (bridges.test.ts).
 */
import type { BridgeProbe, BridgePort, BridgeTarget, SiteBridge } from "../api";

/** RTSP, ONVIF (also PTZ, events and the camera's web page) and Milesight SD replay: what a target carries by default. */
export const DEFAULT_BRIDGE_PORTS = [554, 80, 555];
export const MAX_BRIDGE_PORTS = 8;
const PORT_NAME: Record<number, string> = { 554: "RTSP", 80: "ONVIF", 555: "SD replay", 443: "web", 8554: "RTSP", 8080: "web" };

/** "554 RTSP", "8000" (a port without a usual name). */
export const portName = (p: number) => (PORT_NAME[p] ? `${p} ${PORT_NAME[p]}` : String(p));

/**
 * What the Site's PC runs, in PowerShell opened as administrator in the folder holding axiom-bridge.exe: installs the
 * bridge as a Windows service and enrolls it with the one-time code (hub bridges.install_command says the same; the
 * bridge's own README shows it too).
 */
export function installCommand(hubUrl: string, code: string): string {
  return `.\\axiom-bridge.exe install --hub ${hubUrl} --code ${code}`;
}

/** The hub's /downloads/bridge/latest.json (bridge/publish.ps1 writes it). */
export type BridgeRelease = { version: string; windows?: { file: string; sha256: string } };

/**
 * The same install as one line for an administrator PowerShell, downloading the exe from the hub first: TLS 1.2 on
 * (Windows PowerShell 5.1 may not offer it), no progress bar (it makes iwr crawl on 5.1), the SHA-256 from
 * latest.json checked before anything runs. null when the hub has no Windows release published.
 */
export function oneLineInstall(origin: string, hubUrl: string, code: string, release: BridgeRelease | null): string | null {
  const w = release?.windows;
  if (!w || !/^[0-9a-f]{64}$/i.test(w.sha256) || !/^[A-Za-z0-9._/-]+$/.test(w.file)) return null;
  const url = `${origin.replace(/\/+$/, "")}/downloads/bridge/${w.file}`;
  return [
    "[Net.ServicePointManager]::SecurityProtocol = 'Tls12'",
    "$ProgressPreference = 'SilentlyContinue'",
    "$f = \"$env:TEMP\\axiom-bridge.exe\"",
    `iwr -UseBasicParsing ${url} -OutFile $f`,
    `if ((Get-FileHash $f -Algorithm SHA256).Hash -ne '${w.sha256.toUpperCase()}') { Remove-Item $f; throw 'axiom-bridge.exe: checksum mismatch' }`,
    `& $f install --hub ${hubUrl} --code ${code}`,
  ].join("; ");
}

export type SheetRow = { field: string; label: string; value: string };

const listenOf = (ports: BridgePort[], port: number | null | undefined) => (port ? ports.find((p) => p.port === port)?.listen ?? null : null);

/**
 * The instance camera's outside address for a bridged camera (Apply to camera writes exactly these): the public host is the
 * instance's gateway on its host (its relay), each public port the relay port of the camera's own RTSP / ONVIF / SD replay
 * port (554 / 80 / 555 unless the camera says otherwise). Ports the bridge does not carry are left out; other bridged ports
 * are listed as "Camera port N".
 */
export function relaySheet(target: Pick<BridgeTarget, "ports">, gateway: string | null | undefined,
  cam: { rtsp_port?: number | null; onvif_port?: number | null } = {}): SheetRow[] {
  const rtsp = cam.rtsp_port || 554, onvif = cam.onvif_port || 80;
  const rows: SheetRow[] = [{ field: "public_host", label: "Public host", value: gateway || "not reported yet (host agent 0.3.0 or later)" }];
  const add = (field: string, label: string, port: number) => {
    const l = listenOf(target.ports, port);
    if (l != null) rows.push({ field, label, value: String(l) });
  };
  add("public_rtsp_port", "Public RTSP port", rtsp);
  add("public_onvif_port", "Public ONVIF port", onvif);
  add("public_replay_port", "Public replay (SD) port", 555);
  for (const p of target.ports) {
    if (p.listen != null && ![rtsp, onvif, 555].includes(p.port)) rows.push({ field: `port_${p.port}`, label: `Camera port ${portName(p.port)}`, value: String(p.listen) });
  }
  return rows;
}

/** The sheet as text, for the clipboard. */
export const sheetText = (rows: SheetRow[]) => rows.map((r) => `${r.label}: ${r.value}`).join("\n");

/** 1300000 → "1.3 MB" (decimal, like the rest of the hub). */
export function fmtBytes(n: number | null | undefined): string {
  if (n == null || !isFinite(n) || n < 0) return "—";
  const units = ["B", "kB", "MB", "GB", "TB"];
  let v = n, i = 0;
  while (v >= 1000 && i < units.length - 1) { v /= 1000; i++; }
  return `${i === 0 ? Math.round(v) : v >= 100 ? Math.round(v) : Math.round(v * 10) / 10} ${units[i]}`;
}

/** 7260 → "2 h 1 min", 45 → "45 s". */
export function fmtUptime(s: number | null | undefined): string {
  if (s == null || !isFinite(s) || s < 0) return "—";
  if (s < 60) return `${Math.round(s)} s`;
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `${d} d ${h} h` : h ? `${h} h ${m} min` : `${m} min`;
}

/** The bridge's status line: "online · 2 streams · up 3 h 4 min" or "offline · last seen 12 min ago" / "never connected". */
export function bridgeStatus(b: Pick<SiteBridge, "online" | "streams" | "uptime_s" | "last_seen_at">, now = Date.now() / 1000): string {
  if (b.online) return `online · ${b.streams} stream${b.streams === 1 ? "" : "s"}${b.uptime_s != null ? ` · up ${fmtUptime(b.uptime_s)}` : ""}`;
  if (!b.last_seen_at) return "never connected";
  return `offline · last seen ${fmtUptime(Math.max(0, now - b.last_seen_at))} ago`;
}

/** The last Test: "554 ✓ 3 ms · 80 ✓ 4 ms · 555 ✗ connection refused" (null before any). */
export function probeText(p: BridgeProbe | null | undefined): string | null {
  if (!p || !p.ports.length) return null;
  return p.ports.map((x) => (x.ok ? `${x.port} ✓${x.ms != null ? ` ${Math.round(x.ms)} ms` : ""}` : `${x.port} ✗ ${x.error ?? "no connection"}`)).join(" · ");
}

/** What the bridge said in its last heartbeat about the camera's ports ("554: connection refused"), or null. */
export function targetErrorText(t: Pick<BridgeTarget, "errors">): string | null {
  const e = Object.entries(t.errors ?? {});
  return e.length ? e.map(([port, why]) => `${port}: ${why}`).join(" · ") : null;
}

/** "554 → 47000 · 80 → 47001" for the Site's admins (the listen ports), "554 RTSP · 80 ONVIF" for everyone else. */
export function portsText(ports: BridgePort[]): string {
  return ports.map((p) => (p.listen != null ? `${p.port} → ${p.listen}` : portName(p.port))).join(" · ");
}

/** "554, 80, 555" → [554, 80, 555]; empty = the defaults. */
export function parsePorts(s: string): { ports: number[]; error: null } | { ports: null; error: string } {
  const parts = s.split(/[\s,]+/).filter(Boolean);
  if (!parts.length) return { ports: [...DEFAULT_BRIDGE_PORTS], error: null };
  const out: number[] = [];
  for (const x of parts) {
    const n = Number(x);
    if (!/^\d+$/.test(x) || n < 1 || n > 65535) return { ports: null, error: `${x} is not a TCP port (1-65535)` };
    if (out.includes(n)) return { ports: null, error: `port ${n} is listed twice` };
    out.push(n);
  }
  if (out.length > MAX_BRIDGE_PORTS) return { ports: null, error: `at most ${MAX_BRIDGE_PORTS} ports per camera` };
  return { ports: out, error: null };
}

/** A private (RFC 1918: 10.x, 172.16-31.x, 192.168.x) IPv4 address, as the bridge dials it. */
export function isLanAddress(s: string): boolean {
  const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(s.trim());
  if (!m) return false;
  const [a, b, ...rest] = m.slice(1).map(Number);
  if ([a, b, ...rest].some((x) => x > 255)) return false;
  return a === 10 || (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168);
}

/** The Map a camera form's checks (the hub decides; this keeps the button off until it would accept). */
export function mapFormError(f: { name: string; host: string; ports: string }, bridgeLan: string[] = []): string | null {
  if (!f.name.trim()) return "Give the camera a name";
  if (!isLanAddress(f.host)) return "The camera's LAN address: a private IPv4 address like 192.168.1.50";
  if (bridgeLan.some((a) => a.split("/")[0] === f.host.trim())) return "That is the bridge PC itself";
  const p = parsePorts(f.ports);
  return p.error;
}
