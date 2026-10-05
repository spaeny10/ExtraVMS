/** Hub API client. Same shape as the site's api.ts: relative URLs, cookie session, errors as `${status} ${text}`. */
import type { NvrEvent } from "@site/api";
import type { ActionCardData, ActionExtras, ActionPlanCore, ActionResult } from "@site/ActionCard";
import type { CameraGroup, Dashboard, DashboardConfig, DashboardList, FleetEvent, FleetEvents } from "@site/dashboard/types";
export type { CameraGroup, Dashboard, DashboardConfig, DashboardList, FleetEvent, FleetEvents, Widget, WidgetProps, DashboardWidgetType } from "@site/dashboard/types";
/** /api/fleet/ws. `site_id` on the wire is the server id (the hub kept the name; see the tenancy plan's Naming). */
type LocationTag = { location_id?: string | null; location_name?: string | null };
export type FleetMessage = ({ type: "event"; event: NvrEvent; site_id: string; site_name: string } | { type: "event_removed"; id: number; site_id: string; site_name: string } | { type: "site_online" | "site_offline"; site_id: string; site_name: string }) & LocationTag;

export type Me = { user: { id: string; email: string; totp_enabled: boolean; is_super: boolean }; orgs: Org[]; active_org: string | null };
export type Org = { id: string; name: string; slug: string; role: string };
/**
 * Hierarchy: Customer (wire: org) › Site (wire: location, /api/locations) › Server (wire: site, /api/sites) › Camera.
 * `Server` is what the hub's tables call a "site": one NVR box, one token, one tunnel at /s/<id>/.
 */
export type ServerCamera = { id: string; name: string; stream_ready: boolean; metadata?: boolean; onvif_events?: boolean; bitrate_mbps?: number | null; problems: string[]; ptz?: { at_home: boolean; preset_name: string | null } | null };
export type ServerSummary = {
  now?: number; version?: string; uptime_s?: number; disk?: { free_gb: number; total_gb: number }; retention_alert?: unknown;
  queues?: { verify: number; synopsis: number }; yolo_ready?: boolean; vlm_ready?: boolean; vlm_model?: string;
  cameras?: ServerCamera[]; today?: Record<string, number>; attention?: unknown[]; backup_last?: number | null; bitrate_mbps?: number;
};
/** `location` is the server's own free-text note (older than Sites); `location_id/location_name` is the Site it belongs to. */
export type Server = {
  id: string; org_id: string; name: string; location: string; online: boolean; last_seen_at: number | null; version: string | null; hostname: string | null;
  clock_skew_s: number | null; summary: ServerSummary; open_alerts: number; retired_at?: number | null;
  location_id?: string | null; location_name?: string | null; cameras_total?: number; cameras_online?: number;
};
/** A Site (wire: location) with its rollup and its servers' cards. Retired servers are counted (retired_servers) but left out of `servers` unless asked for. */
export type Site = {
  id: string; org_id: string; name: string; address: string; timezone: string | null; notes: string | null; created_at: number; updated_at: number;
  servers_total: number; servers_online: number; cameras_total: number; cameras_online: number; open_alerts: number; retired_servers: number;
  servers: Server[];
};
/** A row of the hub's cameras registry (synced from server heartbeats). Vanished cameras are kept, with missing_since. */
export type Camera = {
  server_id: string; server_name: string; camera_id: string; name: string; enabled: boolean; stream_ready: boolean; problems: string[] | null;
  bitrate_mbps: number | null; ptz: unknown; last_seen_at: number | null; missing_since: number | null; online: boolean; server_online?: boolean;
};
/**
 * `sites` is the flat list of the customer's visible servers (Home's dashboard, the Sites page's event lookup and many
 * hub tests read it, and it is the only list that includes retired servers); `locations` are the Sites with their
 * servers; `unassigned` = servers in no visible Site.
 */
export type FleetOrg = { org: Org; sites: Server[]; open_alerts: number; retired?: number; locations?: Site[]; unassigned?: Server[] };
export type Fleet = { orgs: FleetOrg[]; now: number; offline_after_s: number };
/** Fleet actions (hub/hub/fleet_actions.py): an Ask-box instruction turned into a plan with a confirmation card (@site/ActionCard). */
export type { ActionCardData, ActionExtras, ActionResult };
export type ActionSiteRef = { id: string; name: string; online: boolean };
export type ActionPlan = { action: "none" } | (ActionPlanCore & {
  summary: string; parser: string; confidence: string;
  source: ActionSiteRef | null; target: ActionSiteRef | null; site: ActionSiteRef | null; cameras: { id: string; name: string; host?: string | null }[];
  days: number | null; new_name: string | null; needs: string[]; expires_at: number; options: Record<string, boolean>;
});
export type ActionVerb = { action: string; title: string; role: string; confirm_name: boolean; examples: string[]; moves: string[]; stays: string[]; undo: string; options: string[]; inputs: string[] };
export type ActionRecent = { id: number; ts: number; user_email: string | null; action: string; status: number | null; lines: string[]; undo_until: number | null };
/** A plan that is an action (the Ask box shows its confirmation card). */
export type ExecPlan = Exclude<ActionPlan, { action: "none" }>;
export type ActionReference = { verbs: ActionVerb[]; safety: string[]; capacity: string[]; recent: ActionRecent[]; undo_hours: number };
export type Alert = { id: number; org_id: string; site_id: string; site_name: string; kind: string; key: string; opened_at: number; closed_at: number | null; acked_by: string | null; detail: Record<string, unknown> } & LocationTag;
export type Member = { id: string; email: string; role: string; totp_enabled: boolean; last_login_at: number | null; all_sites: boolean; location_ids: string[] };
/** What a member may see: every Site of the customer, or only `location_ids` (no implicit "none = all"). */
export type Access = { all_sites: boolean; location_ids: string[] };
export type AuditRow = { id: number; ts: number; user_email: string | null; site_id: string | null; action: string; method: string | null; path: string | null; status: number | null; ip: string | null; undo_until?: number | null } & LocationTag;
export type Usage = { ai_shared: boolean; configured: boolean; model: string; provider: { kind: "site" | "url" | "none"; site_id?: string; site_name?: string | null; online: boolean }; turn: boolean; days: number; sites: { site_id: string; site_name: string; requests: number; prompt_tokens: number; completion_tokens: number; latency_ms: number; errors: number }[] };
/**
 * Where a fan-out result came from. `site_id/site_name` are the server (the wire name predates Sites); newer hubs add
 * the explicit server_* pair and the Site (location_*), so readers fall back to site_id when server_id is missing.
 */
export type ServerTag = { site_id: string; site_name: string; server_id?: string; server_name?: string } & LocationTag;
export type FleetSearchEvent = Record<string, unknown> & ServerTag & { id: number; camera_id: string; start_ts: number; synopsis?: string | null; camera_class: string; snapshot?: string | null };
export type FleetSearch = {
  q: string; sites: (ServerTag & { error: string | null; events: number; footage: number })[]; offline: string[];
  events: FleetSearchEvent[];
  footage: (ServerTag & { camera_id: string; ts: number; score: number })[];
};
/** One server's part of a digest (hub/hub/digest.py collect); `site_id` is the server. */
export type DigestPart = ServerTag & {
  online: boolean; headline: string | null; text: string | null; today: Record<string, number>; cameras_down: string[];
  open_alerts: { kind: string; detail: Record<string, unknown> }[];
};
/** `data` is what the text was written from; `locations` groups its servers by Site (absent on digests older than Sites). */
export type Digest = {
  id: number; org_id: string; day: string; created_at: number; text: string; model: string | null;
  data?: { generated_at?: number; sites?: DigestPart[]; locations?: { id: string | null; name: string; servers: string[] }[] } | null;
};
/** A pending invite link (GET /api/orgs/{org}/invites). `email` "" = anyone holding the link may accept it. */
export type Invite = {
  code: string; url: string; email: string; role: string; all_sites: boolean; location_ids: string[]; label: string | null;
  expires_at: number; created_at: number | null; created_by: string | null; locations?: { id: string; name: string }[]; created_by_email?: string | null;
};
/** The public preview shown on /invite/<code> before anyone signs in; the address is masked (s***@example.com). */
export type InvitePreview = { org_name: string; role: string; all_sites: boolean; locations: { id: string; name: string }[]; email_hint: string; expires_at: number; label: string | null };
export type InviteAccepted = { totp_required?: boolean; user?: Me["user"]; orgs?: Org[]; org_id?: string; role?: string } & Partial<Access>;
export type Backup = { id: number; created_at: number; bytes: number; cameras: number; identities: number; site_version: string | null };
/** A hub administrator (users.is_super): owner of every customer, managed under Account (hub admins only). */
export type HubAdmin = { id: string; email: string; totp_enabled: boolean; last_login_at: number | null };
/** /api/hub/sites: one customer's Sites (same rollups as /api/orgs/{org}/locations) for the "All customers" view. */
export type HubSitesOrg = { org: { id: string; name: string }; locations: Site[]; unassigned: Server[] };
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
  fleet: (org?: string, include_retired?: boolean) => req<Fleet>(`/api/fleet?${qs({ org, include_retired: include_retired || undefined })}`),
  retireServer: (id: string, retired: boolean) => req<Server>(`/api/sites/${id}/retire`, json("POST", { retired })),
  actionPlan: (org: string, text: string) => req<ActionPlan>(`/api/orgs/${org}/actions/plan`, json("POST", { text })),
  /** extras.inputs carries a new camera's password: sent in this one call, never stored. */
  actionExecute: (org: string, plan_id: string, x?: ActionExtras) =>
    req<ActionResult>(`/api/orgs/${org}/actions/execute`, json("POST", { plan_id, confirm_name: x?.confirm_name || undefined, options: x?.options, camera: x && Object.keys(x.inputs).length ? x.inputs : undefined })),
  actionUndo: (org: string, audit_id: number) => req<ActionResult>(`/api/orgs/${org}/actions/undo/${audit_id}`, { method: "POST" }),
  actionReference: (org: string) => req<ActionReference>(`/api/orgs/${org}/actions/reference`),
  orgs: () => req<Org[]>("/api/orgs"),
  createOrg: (name: string, slug: string) => req<Org>("/api/orgs", json("POST", { name, slug })),
  members: (org: string) => req<Member[]>(`/api/orgs/${org}/members`),
  addMember: (org: string, b: { email: string; role: string; password?: string } & Partial<Access>) => req(`/api/orgs/${org}/members`, json("POST", b)),
  removeMember: (org: string, uid: string) => req(`/api/orgs/${org}/members/${uid}`, { method: "DELETE" }),
  setAccess: (org: string, uid: string, b: Access) => req<Access>(`/api/orgs/${org}/members/${uid}/access`, json("PUT", b)),
  servers: (org: string) => req<Server[]>(`/api/orgs/${org}/sites`),
  claimPreview: (code: string) => req<ClaimPreview>(`/api/claims/${encodeURIComponent(code)}`),
  /** location_id omitted: the hub creates a one-server Site named after the server. */
  claim: (org: string, b: { code: string; name: string; location: string; location_id?: string }) => req<Server>(`/api/orgs/${org}/sites/claim`, json("POST", b)),
  /** location_id moves the server to another Site of the same customer. */
  updateServer: (id: string, b: { name?: string; location?: string; location_id?: string }) => req<Server>(`/api/sites/${id}`, json("PATCH", b)),
  rotateServer: (id: string) => req(`/api/sites/${id}/rotate-token`, { method: "POST" }),
  removeServer: (id: string) => req(`/api/sites/${id}`, { method: "DELETE" }),
  // Sites (wire: locations)
  locations: (org: string, include_retired?: boolean) => req<Site[]>(`/api/orgs/${org}/locations?${qs({ include_retired: include_retired || undefined })}`),
  location: (id: string, include_retired?: boolean) => req<Site>(`/api/locations/${id}?${qs({ include_retired: include_retired || undefined })}`),
  createLocation: (org: string, b: { name: string; address?: string; timezone?: string }) => req<Site>(`/api/orgs/${org}/locations`, json("POST", b)),
  updateLocation: (id: string, b: { name?: string; address?: string; timezone?: string | null; notes?: string | null }) => req<Site>(`/api/locations/${id}`, json("PATCH", b)),
  /** 409 while servers remain unless moveTo names another Site of the customer. */
  deleteLocation: (id: string, moveTo?: string) => req<{ ok: boolean; moved: number }>(`/api/locations/${id}?${qs({ move_to: moveTo })}`, { method: "DELETE" }),
  locationCameras: (id: string) => req<Camera[]>(`/api/locations/${id}/cameras`),
  /** location narrows to one Site's servers (same rows as locationAlerts). */
  alerts: (org: string, open = true, location?: string) => req<Alert[]>(`/api/alerts?${qs({ org, open, location })}`),
  locationAlerts: (id: string, open = true, limit?: number) => req<Alert[]>(`/api/locations/${id}/alerts?${qs({ open, limit })}`),
  /** The latest events across one Site's servers (same params and shape as fleetEvents). */
  locationEvents: (id: string, p: { cameras?: { site: string; camera: string }[]; classes?: string[]; limit?: number; since?: number } = {}) =>
    req<FleetEvents>(`/api/locations/${id}/events?${qs({ cameras: p.cameras?.map((c) => `${c.site}:${c.camera}`).join(","), classes: p.classes?.join(","), limit: p.limit, since: p.since })}`),
  ack: (id: number) => req(`/api/alerts/${id}/ack`, { method: "POST" }),
  audit: (org: string, site?: string) => req<AuditRow[]>(`/api/audit?${qs({ org, site })}`),
  usage: (org: string, days = 30) => req<Usage>(`/api/orgs/${org}/usage?${qs({ days })}`),
  fleetSearch: (org: string, q: string, since?: number, location?: string) => req<FleetSearch>(`/api/fleet/search?${qs({ org, q, since, location })}`),
  // invites: links an admin hands out (nothing is emailed); the code in the link is the secret
  invites: (org: string) => req<Invite[]>(`/api/orgs/${org}/invites`),
  createInvite: (org: string, b: { email?: string; role: string; label?: string; expires_days: number } & Access) => req<Invite>(`/api/orgs/${org}/invites`, json("POST", b)),
  revokeInvite: (org: string, code: string) => req(`/api/orgs/${org}/invites/${encodeURIComponent(code)}`, { method: "DELETE" }),
  invitePreview: (code: string) => req<InvitePreview>(`/api/invites/${encodeURIComponent(code)}`),
  /** Signed in: no body needed (the invite joins this account). Otherwise email + password (+ totp when asked for). */
  acceptInvite: (code: string, b: { email?: string; password?: string; totp?: string } = {}) => req<InviteAccepted>(`/api/invites/${encodeURIComponent(code)}/accept`, json("POST", b)),
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
  hubAdmins: () => req<HubAdmin[]>("/api/hub/admins"),
  addHubAdmin: (email: string) => req<HubAdmin>("/api/hub/admins", json("POST", { email })),
  removeHubAdmin: (uid: string) => req(`/api/hub/admins/${encodeURIComponent(uid)}`, { method: "DELETE" }),
  hubAudit: (limit = 20) => req<AuditRow[]>(`/api/hub/audit?${qs({ limit })}`),
  hubSites: (include_retired?: boolean) => req<HubSitesOrg[]>(`/api/hub/sites?${qs({ include_retired: include_retired || undefined })}`),
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

/**
 * Fleet Ask: every server's assistant answers; onChunk gets {site, site_name, server_*, location_*, ...chunk} lines
 * (`site` = the server id). `location` asks only that Site's servers.
 */
export async function fleetAsk(org: string, message: string, onChunk: (c: Record<string, unknown>) => void, location?: string) {
  const r = await fetch("/api/fleet/ask", json("POST", { org, message, location }));
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
