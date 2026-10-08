/** Hub API client. Same shape as the site's api.ts: relative URLs, cookie session, errors as `${status} ${text}`. */
import type { NvrEvent, SavedFindView } from "@site/api";
import type { ActionCardData, ActionExtras, ActionPlanCore, ActionResult } from "@site/ActionCard";
import { type AddressParts, type GeoHit, type GeocodeSource, type MapConfig, setMapConfig } from "./place";
import type { CameraGroup, Dashboard, DashboardConfig, DashboardList, FleetEvent, FleetEvents } from "@site/dashboard/types";
export type { CameraGroup, Dashboard, DashboardConfig, DashboardList, FleetEvent, FleetEvents, Widget, WidgetProps, DashboardWidgetType } from "@site/dashboard/types";
/** /api/fleet/ws. `site_id` on the wire is the server id (the hub kept the name; see the tenancy plan's Naming). */
type LocationTag = { location_id?: string | null; location_name?: string | null };
export type FleetMessage = ({ type: "event"; event: NvrEvent; site_id: string; site_name: string } | { type: "event_removed"; id: number; site_id: string; site_name: string } | { type: "site_online" | "site_offline"; site_id: string; site_name: string }) & LocationTag;

/**
 * `soc_role`: the hub-level SOC role (like is_super, not per customer); null/absent = not SOC staff.
 * `soc_only` (newer hubs): SOC staff with no real customer membership, i.e. their landing page is the console.
 */
export type SocRole = "operator" | "supervisor";
export type Me = { user: { id: string; email: string; totp_enabled: boolean; is_super: boolean; soc_role?: SocRole | null; soc_only?: boolean }; orgs: Org[]; active_org: string | null;
  /** the hub's map tiles (HUB_MAP_TILES; newer hubs) */
  map?: MapConfig;
  /** cellular coverage (CoverageMap; newer hubs): off, or the plan and whether this user sees it anywhere */
  coverage?: CoverageInfo };
/** `visible`: this user sees coverage at all (trial: hub administrators only); `cost_per_lookup`: worst-case units of one lookup. */
export type CoverageInfo = { enabled: boolean; plan: "trial" | "paid" | null; visible: boolean; evaluation: boolean; cost_per_lookup: number };
/**
 * `soc`: the customer is listed only because it has a SOC-monitored Site (no real membership). Newer hubs may send
 * `member` (true = a real membership) alongside it.
 */
export type Org = { id: string; name: string; slug: string; role: string; soc?: boolean; member?: boolean };
/**
 * Hierarchy: Customer (wire: org) › Site (wire: location, /api/locations) › Server (wire: site, /api/sites) › Camera.
 * `Server` is what the hub's tables call a "site": one NVR box, one token, one tunnel at /s/<id>/.
 */
/** `link_down`: unreachable because the whole site link is down (summary.site_link_down); no camera_down then. */
export type ServerCamera = { id: string; name: string; stream_ready: boolean; metadata?: boolean; onvif_events?: boolean; bitrate_mbps?: number | null; problems: string[]; ptz?: { at_home: boolean; preset_name: string | null } | null; link_down?: boolean };
/** What a server pulls from its cameras (newer servers): now, today and this month, and per camera Mbit/s. */
export type Bandwidth = { mbps: number; today_gb?: number; month_gb?: number; cameras?: Record<string, number> };
export type ServerSummary = {
  now?: number; version?: string; uptime_s?: number; disk?: { free_gb: number; total_gb: number }; retention_alert?: unknown;
  queues?: { verify: number; synopsis: number }; yolo_ready?: boolean; vlm_ready?: boolean; vlm_model?: string;
  cameras?: ServerCamera[]; today?: Record<string, number>; attention?: unknown[]; backup_last?: number | null; bitrate_mbps?: number;
  site_link_down?: boolean; bandwidth?: Bandwidth;
};
/** `location` is the server's own free-text note (older than Sites); `location_id/location_name` is the Site it belongs to. */
export type Server = {
  id: string; org_id: string; name: string; location: string; online: boolean; last_seen_at: number | null; version: string | null; hostname: string | null;
  clock_skew_s: number | null; summary: ServerSummary; open_alerts: number; retired_at?: number | null;
  location_id?: string | null; location_name?: string | null; cameras_total?: number; cameras_online?: number;
  /** Direct-on-LAN (newer hubs): the server's own addresses a browser on its LAN can reach without the tunnel. */
  direct?: DirectInfo;
  /** A central recording instance (hub hosts.py): its storage quota and what it uses, from its host's heartbeat. */
  central?: CentralUsage | null;
};
export type CentralUsage = { id: string; mode: CentralMode; quota_gb: number; used_gb: number | null; site_number: number | null; state: string };

// ---- Central recording (hub/hub/hosts.py): datacenter hosts and the per-Site server instances on them
export type CentralMode = "vpn" | "forward";
export type HostGpu = { index: number; name: string; mem_total_mb?: number; mem_used_mb?: number; util?: number };
export type HostCapacity = { cpus?: number; load?: number | number[]; ram_gb?: { total: number; free: number }; gpus?: HostGpu[]; disks?: { path: string; total_gb?: number; free_gb?: number }[]; instances?: number };
/** `offline_since`: when its open host_offline alert opened (null = none open). */
export type Host = {
  id: string; name: string; created_at: number; online: boolean; last_seen_at: number | null; hostname: string | null; version: string | null;
  capacity: HostCapacity | null; notes: string | null; fusionhub: string | null; agent_ip: string | null; instances: number; quota_gb: number; offline_since: number | null;
  /** GB a new instance's quota may take: the largest disk's free space minus what its instances may still grow into. */
  room_gb?: number;
};
/** What the Peplink settings sheet shows (central.ts lays out the port-forward table from the bases). */
/** `forward_addresses`: the routers' public IPs and DNS names the port-forward table applies to (from the camera network). */
export type Peplink = { mode: CentralMode; subnet: string | null; lan_gateway: string | null; public_ip: string | null; forward_addresses?: string[]; fusionhub: string | null; datacenter_ip: string | null; rtsp_base: number; onvif_base: number };
/** The instance's Site networks (set by hub administrators): LAN / VPN subnets (any protocol), public IPs and DNS names (TCP). */
export type CameraNetwork = { subnets: string[]; public_ips: string[]; hosts: string[] };
/**
 * The firewall on the host = the Site networks (subnets / public_ips / hosts) plus `auto`: the public IPs and DNS names the
 * instance's cameras use, opened and closed by the hub as cameras are added (hub central_cameras.py). `auto_on` false: an
 * instance from before automatic addresses (they start once its Site networks are saved). `resolved`: each DNS name's IPv4
 * addresses as the host last resolved them. `pending` / `sync_error` (hub administrators): not on the host yet, and why.
 */
export type CentralCameraNetwork = CameraNetwork & {
  auto?: { public_ips: string[]; hosts: string[] }; auto_on?: boolean; resolved?: Record<string, string[] | null> | null;
  pending?: boolean; sync_error?: string | null;
};
/** One Site's central instance. The host_* / gpu / last_error fields are sent to hub administrators only. */
export type CentralInstance = {
  id: string; location_id: string; location_name: string | null; org_id: string; org_name: string | null; server_id: string | null; server_online: boolean;
  name: string; mode: CentralMode; subnet: string | null; public_ip: string | null; site_number: number | null; quota_gb: number; used_gb: number | null;
  state: "provisioning" | "running" | "failed" | "deleting" | "deleted"; phase: string; created_at: number; ready_at: number | null; info_at: number | null;
  peplink: Peplink; cameras?: { id: string; name: string }[]; camera_network?: CentralCameraNetwork;
  /** The most cameras it may have (null = no limit) and how many it has (enabled, as last reported). */
  camera_limit?: number | null; camera_count?: number;
  host_id?: string; host_name?: string | null; host_online?: boolean; gpu?: number | null; gpu_name?: string | null; last_error?: string | null; host_state?: string | null;
};
/** Read-only on a Site page; `can_manage` (hub administrators) shows the link to the Hosts page, where instances are managed. */
export type LocationCentral = { instances: CentralInstance[]; can_provision?: boolean; can_manage?: boolean };
export type CentralBody = { host_id?: string | null; mode: CentralMode; subnet?: string | null; public_ip?: string | null; quota_gb: number; gpu?: number | null; name?: string | null; camera_limit?: number | null };
/**
 * `urls`: candidate base URLs of the server itself (`local` = only works from a browser on that machine, e.g.
 * http://localhost:8080); `fingerprint`: its self-signed certificate's SHA-256, for the "accept the certificate" prompt.
 */
export type DirectInfo = { available: boolean; urls: { url: string; local?: boolean }[]; fingerprint: string | null };
// (direct.candidates also accepts bare URL strings in `urls`: that is how the server's own heartbeat lists LAN URLs)
/** POST /api/servers/{id}/direct-token: a short-lived token the server accepts for read-only + WHEP requests. */
export type DirectToken = DirectInfo & { token: string; exp: number; role: string };
// ---- Cellular coverage (hub/hub/coverage.py, the CoverageMap API), normalized per carrier and technology
/** One speed-test metric at the nearest radius with successful tests ("closest" = the closest tested area, up to 10 km). */
export type CoverageMetric = { radius: "0.5km" | "1km" | "2km" | "closest"; med: number | null; min: number | null; avg: number | null; max: number | null;
  count: number; failed: number; accuracy: string | null; distance_km?: number | null };
export type CoverageEntry = {
  technology: string; technology_name: string | null;
  summary: { overall: number | null; performance: number | null; coverage: number | null; reliability: number | null; is_fully_covered: boolean; source: "measured" | "estimated" | string | null; accuracy: string | null } | null;
  /** FCC signal (dBm) at the point and averaged within 0.5/1/2 km; coverage = the covered share of each radius (0..1) */
  fcc: { signal: { point: number | null; r05: number | null; r1: number | null; r2: number | null }; coverage: { r05: number | null; r1: number | null; r2: number | null } } | null;
  speed: { download: CoverageMetric | null; upload: CoverageMetric | null; latency: CoverageMetric | null } | null;
};
export type CoverageCarrier = { code: string; name: string; best: number | null; best_technology: string | null; tech: Record<string, CoverageEntry> };
export type CoverageData = { latitude: number | null; longitude: number | null; address?: string | null; confidence?: string | null; error?: string | null; carriers: CoverageCarrier[] };
/** What the Site's cameras push up the link (Mbit/s): measured per camera where the server reports it, else assumed. */
export type CameraNeed = { mbps: number; cameras: number; measured: number; assumed: number; typical: boolean };
export type FitKind = "fits" | "tight" | "wont_fit" | "unknown";
export type UploadFit = { fit: FitKind; ratio: number | null; reason: string };
/** GET /api/locations/{id}/coverage. `data` null: not looked up yet, or (`hidden`) evaluation-era data customers may not see. */
export type SiteCoverage = {
  enabled: boolean; plan: "trial" | "paid"; evaluation: boolean; location_id: string; data: CoverageData | null; fetched_at: number | null;
  units: number | null; plan_at_fetch: string | null; basis: "point" | "address" | null; information: string[]; hidden: boolean;
  stale: boolean; stale_reason: string | null; error: string | null; attempted_at: number | null; can_refresh: boolean; refresh_cost: number;
  refresh_wait_s: number | null; locatable: boolean; need: CameraNeed; fits: Record<string, Record<string, UploadFit>>; source: string;
};
export type CoverageCheck = { cached: boolean; fetched_at: number; units: number; data: CoverageData; evaluation: boolean; need: CameraNeed;
  fits: Record<string, Record<string, UploadFit>>; cost: number; source: string };
export type CoverageUsage = { enabled: boolean; plan: "trial" | "paid" | null; budget: number; month: string; units: number; calls: number; remaining: number | null;
  budget_reached: boolean; alert_open: boolean; history: { month: string; units: number; calls: number }[]; stored_sites: number; datasets: string[];
  refresh_days: number; cost_per_lookup: number };
/** A Site (wire: location) with its rollup and its servers' cards. Retired servers are counted (retired_servers) but left out of `servers` unless asked for. */
export type Site = {
  id: string; org_id: string; name: string; address: string; timezone: string | null; notes: string | null; created_at: number; updated_at: number;
  servers_total: number; servers_online: number; cameras_total: number; cameras_online: number; open_alerts: number; retired_servers: number;
  servers: Server[];
  /** SOC monitoring opted in (newer hubs only; absent = ask /monitoring). */
  monitored?: boolean;
  /** Where the Site is (newer hubs; null = not located yet). `address` stays the one-line address people read. */
  lat?: number | null; lon?: number | null; address_parts?: AddressParts | null; geocoded_at?: number | null; geocode_source?: GeocodeSource | null;
};
/** The place fields a Site create/update may carry (lat and lon together; both null clears the point). */
export type PlaceBody = { lat?: number | null; lon?: number | null; address_parts?: AddressParts | null; geocode_source?: GeocodeSource };
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
/** Fleet actions (hub/hub/fleet_actions.py): an instruction typed on Customer › Actions turned into a plan with a confirmation card (@site/ActionCard). */
export type { ActionCardData, ActionExtras, ActionResult };
export type ActionSiteRef = { id: string; name: string; online: boolean };
export type ActionPlan = { action: "none" } | (ActionPlanCore & {
  summary: string; parser: string; confidence: string;
  source: ActionSiteRef | null; target: ActionSiteRef | null; site: ActionSiteRef | null; cameras: { id: string; name: string; host?: string | null }[];
  days: number | null; new_name: string | null; needs: string[]; expires_at: number; options: Record<string, boolean>;
});
export type ActionVerb = { action: string; title: string; role: string; confirm_name: boolean; examples: string[]; moves: string[]; stays: string[]; undo: string; options: string[]; inputs: string[] };
/** One Action log line: outcome done / failed / refused (with the reason); `can_undo` when this user may undo it now. */
export type ActionRecent = {
  id: number; ts: number; user_email: string | null; action: string; status: number | null; lines: string[]; undo_until: number | null;
  outcome?: "done" | "failed" | "refused"; reason?: string | null; servers?: string[]; location?: string | null; can_undo?: boolean;
  undone_at?: number | null; undone_by?: string | null; undo_of?: number | null;
};
/** A plan that is an action (Customer › Actions shows its confirmation card). */
export type ExecPlan = Exclude<ActionPlan, { action: "none" }>;
/** `log_scope`: "all" (admins: the customer's last 50 actions) or "own" (everyone else: their own). */
export type ActionReference = { verbs: ActionVerb[]; safety: string[]; capacity: string[]; recent: ActionRecent[]; undo_hours: number; log_scope?: "all" | "own" };
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
/** /api/locations/{id}/find/events|search: events tagged with their server; `next` is the opaque cursor (null = all). */
export type SiteFindPage = {
  events: (NvrEvent & ServerTag)[]; next: string | null; offline: string[];
  errors: { server_id: string; server_name: string; error: string }[];
};
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
  /** cut down to the Sites this member can see (a Site-restricted member: the hub rebuilds the text from those servers) */
  scoped?: boolean;
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

// ---- SOC monitoring of a Site (hub/hub/soc_api.py). Times are "HH:MM" in the Site's timezone.
/** One weekly window: `dow` 0 = Monday … 6 = Sunday; `to <= from` runs overnight into the next day. */
export type ArmWindow = { dow: number[]; from: string; to: string };
/** A date that replaces the weekly schedule: armed all day, disarmed all day, or armed only from–to. */
export type ArmHoliday = { date: string; name: string; armed: boolean; from?: string; to?: string };
/** A manual arm/disarm until `until` (the hub stores mode "arm" | "disarm"; an override past `until` no longer counts). */
export type ArmOverride = { mode: "arm" | "disarm"; until: number; by: string | null; by_id?: string | null; reason: string; at: number };
/** What the Settings editor PUTs back. */
export type MonitoringConfig = { monitored: boolean; arm_schedule: ArmWindow[]; arm_holidays: ArmHoliday[]; soc_group_minutes: number | null };
/** Why the hub says armed or not (soc.compute_armed). */
export type ArmReason = "unmonitored" | "override" | "holiday" | "schedule" | "disarmed_schedule" | "always";
/**
 * GET adds the override and the hub's verdict for right now: `next_change` = when it next flips (null = never within
 * the horizon), `can_configure` / `can_arm` = what this user may do here.
 */
export type Monitoring = MonitoringConfig & {
  arm_override: ArmOverride | null; override_active?: boolean; armed: boolean; reason: ArmReason | string; next_change: { at: number; armed: boolean } | null;
  timezone?: string | null; now?: number; can_configure?: boolean; can_arm?: boolean;
};
/** Blank optional fields travel as null (the hub stores NULL); the editor works on "" and normalizes on load. */
export type SiteContact = { id?: number; order: number; name: string; role: string | null; phone: string | null; email: string | null; notify_on_open: boolean; notes: string | null };
/** Step ids are kept by the hub (SOP ticks in incident logs refer to them); a step sent without one gets "s<n>". */
export type ProcedureStep = { id?: string; text: string; required: boolean };
/** `category` null = any incident; `priority` = applies from this priority up, null = every priority. */
export type Procedure = { id?: number; order: number; title: string; category: string | null; steps: ProcedureStep[]; priority: "low" | "medium" | "high" | null };

/** Exported for the SOC client (soc/socApi.ts): same error shape everywhere. */
export async function req<T>(url: string, init?: RequestInit): Promise<T> {
  const r = await fetch(url, init);
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}
export const json = (method: string, body: unknown): RequestInit => ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
export const qs = (p: Record<string, string | number | boolean | undefined | null>) =>
  new URLSearchParams(Object.entries(p).filter(([, v]) => v !== undefined && v !== null && v !== "").map(([k, v]) => [k, String(v)])).toString();

/**
 * Every server card the hub hands out (fleet, a Site, the Sites list) is offered to these listeners, so direct.ts knows
 * each server's direct addresses even on pages that only have a server id (Find, Alerts, the SOC).
 */
type CardListener = (servers: Server[]) => void;
const cardListeners = new Set<CardListener>();
export function onServerCards(fn: CardListener): () => void {
  cardListeners.add(fn);
  return () => { cardListeners.delete(fn); };
}
function seen<T>(p: Promise<T>, pick: (x: T) => Server[]): Promise<T> {
  return p.then((x) => {
    if (cardListeners.size) { try { const s = pick(x); cardListeners.forEach((fn) => fn(s)); } catch { /* an unexpected shape never breaks the page */ } }
    return x;
  });
}

export const api = {
  me: () => req<Me>("/auth/me").then((m) => { setMapConfig(m.map); return m; }),
  directToken: (server: string) => req<DirectToken>(`/api/servers/${server}/direct-token`, { method: "POST" }),
  login: (email: string, password: string, totp?: string) => req<{ totp_required?: boolean } & Partial<Me>>("/auth/login", json("POST", { email, password, totp })),
  logout: () => req("/auth/logout", { method: "POST" }),
  /** Two-factor changes re-check the password, and a current authenticator code while two-factor is on. */
  totpSetup: (password: string, code?: string) => req<{ secret: string; uri: string }>("/auth/totp/setup", json("POST", { password, code: code || null })),
  totpEnable: (code: string) => req("/auth/totp/enable", json("POST", { code })),
  totpDisable: (password: string, code: string) => req("/auth/totp/disable", json("POST", { password, code })),
  password: (current: string, next: string) => req("/auth/password", json("POST", { current, new: next })),
  fleet: (org?: string, include_retired?: boolean) => seen(req<Fleet>(`/api/fleet?${qs({ org, include_retired: include_retired || undefined })}`),
    (f) => f.orgs.flatMap((o) => [...o.sites, ...(o.locations ?? []).flatMap((l) => l.servers), ...(o.unassigned ?? [])])),
  retireServer: (id: string, retired: boolean) => req<Server>(`/api/sites/${id}/retire`, json("POST", { retired })),
  /** Only the Actions page plans; it sends origin "actions_page", the one origin the hub will execute. */
  actionPlan: (org: string, text: string) => req<ActionPlan>(`/api/orgs/${org}/actions/plan`, json("POST", { text, origin: "actions_page" })),
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
  locations: (org: string, include_retired?: boolean) => seen(req<Site[]>(`/api/orgs/${org}/locations?${qs({ include_retired: include_retired || undefined })}`),
    (l) => l.flatMap((s) => s.servers)),
  location: (id: string, include_retired?: boolean) => seen(req<Site>(`/api/locations/${id}?${qs({ include_retired: include_retired || undefined })}`), (s) => s.servers),
  /** Address suggestions ([] when the geocoder is down); 429 past ~30 a minute. */
  geocode: (q: string) => req<GeoHit[]>(`/api/geocode?${qs({ q })}`),
  geocodeReverse: (lat: number, lon: number) => req<GeoHit | null>(`/api/geocode/reverse?${qs({ lat, lon })}`),
  /** The IANA time zone at a point (offline on the hub). */
  geocodeTimezone: (lat: number, lon: number) => req<{ timezone: string | null }>(`/api/geocode/timezone?${qs({ lat, lon })}`),
  createLocation: (org: string, b: { name: string; address?: string; timezone?: string } & PlaceBody) => req<Site>(`/api/orgs/${org}/locations`, json("POST", b)),
  updateLocation: (id: string, b: { name?: string; address?: string; timezone?: string | null; notes?: string | null } & PlaceBody) => req<Site>(`/api/locations/${id}`, json("PATCH", b)),
  /** 409 while servers remain unless moveTo names another Site of the customer. */
  deleteLocation: (id: string, moveTo?: string) => req<{ ok: boolean; moved: number }>(`/api/locations/${id}?${qs({ move_to: moveTo })}`, { method: "DELETE" }),
  locationCameras: (id: string) => req<Camera[]>(`/api/locations/${id}/cameras`),
  /** location narrows to one Site's servers (same rows as locationAlerts). */
  alerts: (org: string, open = true, location?: string) => req<Alert[]>(`/api/alerts?${qs({ org, open, location })}`),
  locationAlerts: (id: string, open = true, limit?: number) => req<Alert[]>(`/api/locations/${id}/alerts?${qs({ open, limit })}`),
  /** The latest events across one Site's servers (same params and shape as fleetEvents). */
  locationEvents: (id: string, p: { cameras?: { site: string; camera: string }[]; classes?: string[]; limit?: number; since?: number } = {}) =>
    req<FleetEvents>(`/api/locations/${id}/events?${qs({ cameras: p.cameras?.map((c) => `${c.site}:${c.camera}`).join(","), classes: p.classes?.join(","), limit: p.limit, since: p.since })}`),
  /** A Site's Find tab: one page of events (browse) or search hits across its servers; `next` goes back as `cursor`. */
  locationFindEvents: (id: string, p: Record<string, string | number | boolean | undefined>) => req<SiteFindPage>(`/api/locations/${id}/find/events?${qs(p)}`),
  locationFindSearch: (id: string, p: Record<string, string | number | boolean | undefined>) => req<SiteFindPage>(`/api/locations/${id}/find/search?${qs(p)}`),
  /** The Site's saved Find views (everyone who sees the Site reads them; operators and up save them). */
  locationFindViews: (id: string) => req<{ views: SavedFindView[]; can_edit: boolean }>(`/api/locations/${id}/find-views`),
  saveLocationFindViews: (id: string, views: SavedFindView[]) => req<{ views: SavedFindView[]; can_edit: boolean }>(`/api/locations/${id}/find-views`, json("PUT", { views })),
  // a Site's Ask: the signed-in user's own conversations (site_ask.py); asking streams (siteAsk below)
  siteAskThreads: (id: string) => req<SiteAskThread[]>(`/api/locations/${id}/ask/threads`),
  siteAskThread: (id: string, tid: number | string) => req<SiteAskThreadFull>(`/api/locations/${id}/ask/threads/${tid}`),
  renameSiteAskThread: (id: string, tid: number, title: string) => req<SiteAskThreadFull>(`/api/locations/${id}/ask/threads/${tid}`, json("PATCH", { title })),
  deleteSiteAskThread: (id: string, tid: number) => req<{ ok: boolean }>(`/api/locations/${id}/ask/threads/${tid}`, { method: "DELETE" }),
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
  // central recording: hosts are hub administrators' business; a Site's admins read its instance and Peplink sheet
  hosts: () => req<{ hosts: Host[]; install: string; host_agent_url: string }>("/api/hub/hosts"),
  /** The token is in this answer only (the hub keeps a hash). */
  addHost: (b: { name: string; notes?: string; fusionhub?: string }) => req<{ host: Host; token: string; install: string }>("/api/hub/hosts", json("POST", b)),
  updateHost: (id: string, b: { name?: string; notes?: string | null; fusionhub?: string | null }) => req<Host>(`/api/hub/hosts/${encodeURIComponent(id)}`, json("PATCH", b)),
  removeHost: (id: string) => req(`/api/hub/hosts/${encodeURIComponent(id)}`, { method: "DELETE" }),
  rotateHost: (id: string) => req<{ token: string; install: string }>(`/api/hub/hosts/${encodeURIComponent(id)}/rotate-token`, { method: "POST" }),
  hubCentral: () => req<CentralInstance[]>("/api/hub/central"),
  locationCentral: (loc: string) => req<LocationCentral>(`/api/locations/${loc}/central`),
  provisionCentral: (loc: string, b: CentralBody) => req<CentralInstance>(`/api/locations/${loc}/central`, json("POST", b)),
  /** Storage quota and / or camera limit (null = no limit); hub administrators. */
  updateCentral: (loc: string, ci: string, b: { quota_gb?: number; camera_limit?: number | null }) => req<CentralInstance>(`/api/locations/${loc}/central/${ci}`, json("PATCH", b)),
  /** Replaces the instance's Site networks; the host gets them plus its cameras' own addresses (hub administrators). */
  setCentralCameras: (loc: string, ci: string, b: CameraNetwork) => req<CentralInstance>(`/api/locations/${loc}/central/${ci}/cameras`, json("PUT", b)),
  /** purge also deletes the recordings on the host; force forgets the instance at the hub when its host can't. */
  removeCentral: (loc: string, ci: string, o: { purge?: boolean; force?: boolean } = {}) =>
    req<CentralInstance>(`/api/locations/${loc}/central/${ci}?${qs({ purge: o.purge || undefined, force: o.force || undefined })}`, { method: "DELETE" }),
  // SOC monitoring per Site: config for customer admins and SOC supervisors, arm/disarm now for operators of either side
  coverage: (loc: string) => req<SiteCoverage>(`/api/locations/${loc}/coverage`),
  refreshCoverage: (loc: string) => req<SiteCoverage>(`/api/locations/${loc}/coverage/refresh`, { method: "POST" }),
  coverageCheck: (b: { lat?: number; lon?: number; address?: string; org_id?: string }) => req<CoverageCheck>("/api/coverage/check", json("POST", b)),
  hubCoverage: () => req<CoverageUsage>("/api/hub/coverage"),
  monitoring: (loc: string) => req<Monitoring>(`/api/locations/${loc}/monitoring`),
  setMonitoring: (loc: string, b: MonitoringConfig) => req<Monitoring>(`/api/locations/${loc}/monitoring`, json("PUT", b)),
  /** `until` epoch seconds, at most 24 h ahead (the hub refuses more); a reason is required. */
  arm: (loc: string, b: { mode: "arm" | "disarm"; until: number; reason: string }) => req<Monitoring>(`/api/locations/${loc}/arm`, json("POST", b)),
  clearArm: (loc: string) => req<Monitoring>(`/api/locations/${loc}/arm`, { method: "DELETE" }),
  contacts: (loc: string) => req<SiteContact[]>(`/api/locations/${loc}/contacts`),
  setContacts: (loc: string, rows: SiteContact[]) => req<SiteContact[]>(`/api/locations/${loc}/contacts`, json("PUT", { contacts: rows })),
  procedures: (loc: string) => req<Procedure[]>(`/api/locations/${loc}/procedures`),
  setProcedures: (loc: string, rows: Procedure[]) => req<Procedure[]>(`/api/locations/${loc}/procedures`, json("PUT", { procedures: rows })),
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

// ---- a Site's Ask tab (hub/hub/site_ask.py)
/** One piece of the merged evidence an answer was written from. `ref` is what the answer cites: [#ref] for events and
 * journeys ("123", or "123a"/"123b" when two servers' events share an id), [ref] for footage ("F1"). */
export type SiteAskSource = {
  ref?: string; kind: "event" | "footage" | "journey" | "gap" | "note" | "briefing";
  server_id: string; server_name: string; event_id?: number; event_ids?: number[]; camera_id?: string; camera?: string; cameras?: string[];
  ts?: number; end_ts?: number; label?: string; priority?: string; snapshot?: boolean; text?: string; synopsis?: string; earlier?: boolean; later?: boolean;
  minutes?: number; ongoing?: boolean; background?: boolean;
};
export type SiteAskServer = { server_id: string; server_name: string; status: "ok" | "offline" | "error" | "timeout"; error?: string; last_seen_at?: number; duration_ms?: number };
export type SiteAskCounts = { events?: number; by_label?: Record<string, number>; by_camera?: Record<string, number>; people?: [number, number] };
export type SiteAskSources = {
  items: SiteAskSource[]; servers: SiteAskServer[]; counts: SiteAskCounts; window: { from: number; to: number; label: string | null } | null;
  question: string | null; dropped: number; fallback?: string;
};
export type SiteAskThread = { id: number; title: string; created_at: number; updated_at: number; messages: number };
export type SiteAskMessage = { id: number; thread_id: number; role: "user" | "assistant"; content: string; sources: SiteAskSources | null; model: string | null; created_at: number; duration_ms: number | null };
export type SiteAskThreadFull = Omit<SiteAskThread, "messages"> & { location_id: string; messages: SiteAskMessage[] };
/** The stream's chunks: thread, user, status, sources, model, delta, fallback, done, instruction, error. */
export type SiteAskChunk =
  | { type: "thread"; thread_id: number } | { type: "user"; id: number } | { type: "status"; text: string; servers: number; online: number }
  | ({ type: "sources" } & SiteAskSources) | { type: "model"; model: string | null } | { type: "delta"; text: string }
  | { type: "fallback"; reason: string } | { type: "done"; id: number | null; duration_ms?: number }
  | { type: "instruction"; text: string; href: string; message: string } | { type: "error"; error: string };

/** Ask a Site (one answer for all its servers); chunks arrive as the hub writes them. */
export async function siteAsk(location: string, body: { question: string; thread_id?: number | null }, onChunk: (c: SiteAskChunk) => void, signal?: AbortSignal) {
  const r = await fetch(`/api/locations/${location}/ask`, { ...json("POST", { question: body.question, thread_id: body.thread_id ?? undefined }), signal });
  if (!r.ok || !r.body) throw new Error(`${r.status} ${await r.text()}`);
  const reader = r.body.getReader(); const dec = new TextDecoder(); let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let nl: number;
    while ((nl = buf.indexOf("\n")) >= 0) { const line = buf.slice(0, nl).trim(); buf = buf.slice(nl + 1); if (line) onChunk(JSON.parse(line) as SiteAskChunk); }
  }
  if (buf.trim()) onChunk(JSON.parse(buf) as SiteAskChunk);
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
