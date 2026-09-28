/** Hub API client. Same shape as the site's api.ts: relative URLs, cookie session, errors as `${status} ${text}`. */
import type { NvrEvent } from "@site/api";
import type { CameraGroup, Dashboard, DashboardConfig, DashboardList, FleetEvent, FleetEvents } from "@site/dashboard/types";
export type { CameraGroup, Dashboard, DashboardConfig, DashboardList, FleetEvent, FleetEvents, Widget, WidgetProps, DashboardWidgetType } from "@site/dashboard/types";
export type FleetMessage = { type: "event"; event: NvrEvent; site_id: string; site_name: string } | { type: "site_online" | "site_offline"; site_id: string; site_name: string };

export type Me = { user: { id: string; email: string; totp_enabled: boolean; is_super: boolean }; orgs: Org[]; active_org: string | null };
export type Org = { id: string; name: string; slug: string; role: string };
export type SiteCamera = { id: string; name: string; stream_ready: boolean; metadata?: boolean; onvif_events?: boolean; bitrate_mbps?: number | null; problems: string[]; ptz?: { at_home: boolean; preset_name: string | null } | null };
export type SiteSummary = {
  now?: number; version?: string; uptime_s?: number; disk?: { free_gb: number; total_gb: number }; retention_alert?: unknown;
  queues?: { verify: number; synopsis: number }; yolo_ready?: boolean; vlm_ready?: boolean; vlm_model?: string;
  cameras?: SiteCamera[]; today?: Record<string, number>; attention?: unknown[]; backup_last?: number | null; bitrate_mbps?: number;
};
export type Site = { id: string; org_id: string; name: string; location: string; online: boolean; last_seen_at: number | null; version: string | null; hostname: string | null; clock_skew_s: number | null; summary: SiteSummary; open_alerts: number };
export type Fleet = { orgs: { org: Org; sites: Site[]; open_alerts: number }[]; now: number; offline_after_s: number };
export type Alert = { id: number; org_id: string; site_id: string; site_name: string; kind: string; key: string; opened_at: number; closed_at: number | null; acked_by: string | null; detail: Record<string, unknown> };
export type Member = { id: string; email: string; role: string; totp_enabled: boolean; last_login_at: number | null; sites: string[] };
export type AuditRow = { id: number; ts: number; user_email: string | null; site_id: string | null; action: string; method: string | null; path: string | null; status: number | null; ip: string | null };
export type Usage = { ai_shared: boolean; configured: boolean; model: string; turn: boolean; days: number; sites: { site_id: string; site_name: string; requests: number; prompt_tokens: number; completion_tokens: number; latency_ms: number; errors: number }[] };
export type FleetSearch = {
  q: string; sites: { site_id: string; site_name: string; error: string | null; events: number; footage: number }[]; offline: string[];
  events: (Record<string, unknown> & { id: number; camera_id: string; start_ts: number; synopsis?: string | null; camera_class: string; site_id: string; site_name: string; snapshot?: string | null })[];
  footage: { camera_id: string; ts: number; score: number; site_id: string; site_name: string }[];
};
export type Digest = { id: number; org_id: string; day: string; created_at: number; text: string; model: string | null };
export type Backup = { id: number; created_at: number; bytes: number; cameras: number; identities: number; site_version: string | null };
export type PushInfo = { public_key: string; subscriptions: { endpoint: string; kinds: string[]; ua: string }[]; kinds: string[] };
export type ClaimPreview = { code: string; hint: { hostname?: string; cameras?: { id: string; name: string }[]; version?: string }; agent_ip: string | null; waiting: boolean };

async function req<T>(url: string, init?: RequestInit): Promise<T> {
  const r = await fetch(url, init);
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}
const json = (method: string, body: unknown): RequestInit => ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const qs = (p: Record<string, string | number | boolean | undefined | null>) =>
  new URLSearchParams(Object.entries(p).filter(([, v]) => v !== undefined && v !== null && v !== "").map(([k, v]) => [k, String(v)])).toString();

export const api = {
  me: () => req<Me>("/auth/me"),
  login: (email: string, password: string, totp?: string) => req<{ totp_required?: boolean } & Partial<Me>>("/auth/login", json("POST", { email, password, totp })),
  logout: () => req("/auth/logout", { method: "POST" }),
  totpSetup: () => req<{ secret: string; uri: string }>("/auth/totp/setup", { method: "POST" }),
  totpEnable: (code: string) => req("/auth/totp/enable", json("POST", { code })),
  totpDisable: () => req("/auth/totp/disable", { method: "POST" }),
  password: (current: string, next: string) => req("/auth/password", json("POST", { current, new: next })),
  fleet: (org?: string) => req<Fleet>(`/api/fleet?${qs({ org })}`),
  orgs: () => req<Org[]>("/api/orgs"),
  createOrg: (name: string, slug: string) => req<Org>("/api/orgs", json("POST", { name, slug })),
  members: (org: string) => req<Member[]>(`/api/orgs/${org}/members`),
  addMember: (org: string, b: { email: string; role: string; password?: string }) => req(`/api/orgs/${org}/members`, json("POST", b)),
  removeMember: (org: string, uid: string) => req(`/api/orgs/${org}/members/${uid}`, { method: "DELETE" }),
  setGrants: (org: string, uid: string, site_ids: string[]) => req(`/api/orgs/${org}/members/${uid}/grants`, json("PUT", { site_ids })),
  sites: (org: string) => req<Site[]>(`/api/orgs/${org}/sites`),
  claimPreview: (code: string) => req<ClaimPreview>(`/api/claims/${encodeURIComponent(code)}`),
  claim: (org: string, b: { code: string; name: string; location: string }) => req<Site>(`/api/orgs/${org}/sites/claim`, json("POST", b)),
  updateSite: (id: string, b: { name?: string; location?: string }) => req<Site>(`/api/sites/${id}`, json("PATCH", b)),
  rotateSite: (id: string) => req(`/api/sites/${id}/rotate-token`, { method: "POST" }),
  removeSite: (id: string) => req(`/api/sites/${id}`, { method: "DELETE" }),
  alerts: (org: string, open = true) => req<Alert[]>(`/api/alerts?${qs({ org, open })}`),
  ack: (id: number) => req(`/api/alerts/${id}/ack`, { method: "POST" }),
  audit: (org: string, site?: string) => req<AuditRow[]>(`/api/audit?${qs({ org, site })}`),
  usage: (org: string, days = 30) => req<Usage>(`/api/orgs/${org}/usage?${qs({ days })}`),
  fleetSearch: (org: string, q: string, since?: number) => req<FleetSearch>(`/api/fleet/search?${qs({ org, q, since })}`),
  // home dashboards, camera groups, fleet events
  dashboards: (org: string) => req<DashboardList>(`/api/orgs/${org}/dashboards`),
  dashboard: (org: string, id: string) => req<Dashboard & { can_edit: boolean }>(`/api/orgs/${org}/dashboards/${id}`),
  createDashboard: (org: string, b: { name: string; config: DashboardConfig; shared?: boolean }) => req<Dashboard & { can_edit: boolean }>(`/api/orgs/${org}/dashboards`, json("POST", b)),
  updateDashboard: (org: string, id: string, b: { name?: string; config?: DashboardConfig; shared?: boolean }) => req<Dashboard & { can_edit: boolean }>(`/api/orgs/${org}/dashboards/${id}`, json("PUT", b)),
  deleteDashboard: (org: string, id: string) => req(`/api/orgs/${org}/dashboards/${id}`, { method: "DELETE" }),
  setDefaultDashboard: (org: string, id: string | null) => req<{ default_id: string | null }>(`/api/orgs/${org}/dashboards/default`, json("PUT", { id })),
  groups: (org: string) => req<CameraGroup[]>(`/api/orgs/${org}/groups`),
  createGroup: (org: string, b: { name: string; members: { site: string; camera: string }[] }) => req<CameraGroup>(`/api/orgs/${org}/groups`, json("POST", b)),
  updateGroup: (org: string, id: string, b: { name?: string; members?: { site: string; camera: string }[] }) => req<CameraGroup>(`/api/orgs/${org}/groups/${id}`, json("PUT", b)),
  deleteGroup: (org: string, id: string) => req(`/api/orgs/${org}/groups/${id}`, { method: "DELETE" }),
  fleetEvents: (org: string, p: { sites?: string[]; cameras?: { site: string; camera: string }[]; group?: string; classes?: string[]; limit?: number; since?: number }) =>
    req<FleetEvents>(`/api/fleet/events?${qs({ org, sites: p.sites?.join(","), cameras: p.cameras?.map((c) => `${c.site}:${c.camera}`).join(","), group: p.group, classes: p.classes?.join(","), limit: p.limit, since: p.since })}`),
  digests: (org: string) => req<Digest[]>(`/api/orgs/${org}/digests`),
  digestNow: (org: string) => req<Digest>(`/api/orgs/${org}/digests/generate`, { method: "POST" }),
  backups: (site: string) => req<Backup[]>(`/api/sites/${site}/backups`),
  backupNow: (site: string) => req<Backup>(`/api/sites/${site}/backups`, { method: "POST" }),
  restore: (site: string, id: number, replace_identities = false) => req<Record<string, number>>(`/api/sites/${site}/backups/${id}/restore`, json("POST", { replace_identities })),
  pushInfo: () => req<PushInfo>("/api/push/vapid"),
  pushSubscribe: (subscription: unknown, kinds: string[]) => req("/api/push/subscribe", json("POST", { subscription, kinds })),
  pushUnsubscribe: (endpoint: string) => req("/api/push/unsubscribe", json("POST", { endpoint })),
  patchOrg: (org: string, b: { name?: string; ai_shared?: boolean }) => req<Org>(`/api/orgs/${org}`, json("PATCH", b)),
};

/** Live events from every site of the org (and site online/offline); reconnects with backoff. */
export function subscribeFleet(org: string, onMessage: (m: FleetMessage) => void, onStatus?: (up: boolean) => void): () => void {
  let ws: WebSocket | null = null;
  let closed = false;
  let retry = 1000;
  const connect = () => {
    ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/fleet/ws?org=${encodeURIComponent(org)}`);
    ws.onmessage = (m) => onMessage(JSON.parse(m.data));
    ws.onopen = () => { retry = 1000; onStatus?.(true); };
    ws.onclose = () => { onStatus?.(false); if (!closed) setTimeout(connect, (retry = Math.min(retry * 2, 15000))); };
  };
  connect();
  return () => { closed = true; ws?.close(); };
}

/** Fleet Ask: every site's assistant answers; onChunk gets {site, site_name, ...chunk} lines. */
export async function fleetAsk(org: string, message: string, onChunk: (c: Record<string, unknown>) => void) {
  const r = await fetch("/api/fleet/ask", json("POST", { org, message }));
  if (!r.ok || !r.body) throw new Error(`${r.status} ${await r.text()}`);
  const reader = r.body.getReader(); const dec = new TextDecoder(); let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let nl: number;
    while ((nl = buf.indexOf("\n")) >= 0) { const line = buf.slice(0, nl).trim(); buf = buf.slice(nl + 1); if (line) onChunk(JSON.parse(line)); }
  }
}

export const fmtTime = (ts: number) =>
  new Date(ts * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
export const ago = (ts: number | null | undefined, now = Date.now() / 1000) => {
  if (!ts) return "never";
  const s = Math.max(0, now - ts);
  return s < 90 ? `${Math.round(s)} s ago` : s < 5400 ? `${Math.round(s / 60)} min ago` : s < 172800 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} d ago`;
};
