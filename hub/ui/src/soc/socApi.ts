/** SOC client (hub/hub/soc_api.py): the same req/json/qs as api.ts, so errors read `${status} ${text}` everywhere. */
import { json, qs, req } from "../api";
import { normFalseAlarms, normOperators } from "./reports/normalize";
import type {
  CallOutcome, CustomerSummary, Dispositions, Incident, IncidentDetail, Overview, Presence, PresenceStatus, Priority,
  ShiftReport, SlaPolicy, SlaRule, SocMessage, SocSite,
} from "./types";

const inc = (id: number | string, verb: string) => `/api/soc/incidents/${encodeURIComponent(String(id))}/${verb}`;
const send = <T>(url: string, body?: unknown) => req<T>(url, body === undefined ? { method: "POST" } : json("POST", body));
/** Every incident action answers {incident, ...extras}; callers want the row (the queue applies it at once). */
const post = (url: string, body?: unknown) => send<{ incident: Incident } | Incident>(url, body).then((r) => ("incident" in r && r.incident ? r.incident : r as Incident));

export type IncidentQuery = { state?: string; lane?: string; org?: string; location?: string; since?: number; limit?: number };

export const socApi = {
  incidents: (q: IncidentQuery = {}) => req<Incident[]>(`/api/soc/incidents?${qs(q)}`),
  incident: (id: number | string) => req<IncidentDetail>(`/api/soc/incidents/${encodeURIComponent(String(id))}`),
  claim: (id: number) => post(inc(id, "claim")),
  release: (id: number) => post(inc(id, "release")),
  takeover: (id: number) => post(inc(id, "takeover")),
  promote: (id: number) => post(inc(id, "promote")),
  verify: (id: number) => post(inc(id, "verify")),
  handoff: (id: number, user_id: string) => post(inc(id, "handoff"), { user_id }),
  resolve: (id: number, disposition: string, notes: string) => post(inc(id, "resolve"), { disposition, notes }),
  note: (id: number, text: string) => post(inc(id, "note"), { text }),
  call: (id: number, contact_id: number, outcome: CallOutcome, notes = "") => post(inc(id, "calls"), { contact_id, outcome, notes: notes || undefined }),
  sop: (id: number, procedure_id: number, step_id: string, done: boolean, note?: string) => post(inc(id, "sop"), { procedure_id, step_id, done, note }),
  /** A failed switch is an error (503 server offline, 502 the site refused) and is logged on the incident either way. */
  relay: (id: number, server_id: string, camera_id: string, on: boolean) => post(inc(id, "relay"), { server_id, camera_id, on }),
  /** Quiet lane in bulk (optionally one Site): every unclaimed quiet incident there is closed as "swept". */
  sweep: (location_id?: string) => send<{ swept: number[]; count: number }>("/api/soc/incidents/sweep", location_id ? { location_id } : {}),
  dispositions: () => req<Dispositions>("/api/soc/dispositions"),
  presence: () => req<Presence[]>("/api/soc/presence"),
  /** Answers my own roster entry (a heartbeat with an unchanged status only refreshes last_seen_at). */
  setPresence: (status: PresenceStatus) => req<Presence | Presence[]>("/api/soc/presence", json("PUT", { status })),
  sla: () => req<{ sla: SlaPolicy; defaults?: SlaPolicy } | SlaPolicy>("/api/soc/sla").then((r) => ("sla" in r && r.sla ? r.sla : r as SlaPolicy)),
  sites: () => req<SocSite[]>("/api/soc/sites"),
  /** Customer side: the SOC incidents at one Site, each with its log (read-only). */
  locationIncidents: (loc: string, p: { since?: number; limit?: number } = {}) => req<Incident[]>(`/api/locations/${encodeURIComponent(loc)}/incidents?${qs(p)}`),
};

/**
 * The SOC socket: a snapshot first, then incident/presence/arming frames. Reconnects with the same backoff as
 * subscribeFleet (1 s doubling to 15 s, reset once a connection opens); `onStatus(true)` on every (re)open lets the
 * caller resync anything a dropped connection may have missed.
 */
export const WS_FATAL = new Set([4401, 4403]);

export function subscribeSoc(onMessage: (m: SocMessage) => void, onStatus?: (up: boolean, fatal?: number) => void): () => void {
  let ws: WebSocket | null = null;
  let closed = false;
  let retry = 1000;
  let timer: ReturnType<typeof setTimeout> | null = null;
  const connect = () => {
    ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/soc/ws`);
    ws.onmessage = (m) => { try { onMessage(JSON.parse(m.data)); } catch { /* a malformed frame must not kill the console */ } };
    ws.onopen = () => { retry = 1000; onStatus?.(true); };
    ws.onclose = (e) => {
      // 4401 signed out, 4403 not (or no longer) SOC staff: retrying can't help, so say so once and stop
      if (WS_FATAL.has(e.code)) { onStatus?.(false, e.code); return; }
      onStatus?.(false);
      if (!closed) timer = setTimeout(connect, (retry = Math.min(retry * 2, 15000)));
    };
  };
  connect();
  return () => { closed = true; if (timer) clearTimeout(timer); ws?.close(); };
}

/** Supervisor view and reports (stages 3 and 5). */
export const socSupervisorApi = {
  overview: () => req<Overview>("/api/soc/overview"),
  /** {sla, defaults}: the editor shows the defaults next to what is in force */
  slaFull: () => req<{ sla: SlaPolicy; defaults?: SlaPolicy }>("/api/soc/sla"),
  /** Partial: per priority only the fields sent change; a priority sent as null goes back to its defaults. */
  putSla: (patch: Partial<Record<Priority, Partial<SlaRule> | null>>) => req<{ sla: SlaPolicy; defaults?: SlaPolicy }>("/api/soc/sla", json("PUT", patch)),
  /** Send a resolution awaiting verification back to its operator, with the reason. */
  reject: (id: number, note: string) => post(inc(id, "reject"), { note }),
};

export type ReportQuery = { since?: number; until?: number; org?: string };
export const socReportsApi = {
  /** {operators, totals}; soc_reports.py nests the percentiles, normalize.ts flattens them */
  operators: (q: ReportQuery = {}) => req<unknown>(`/api/soc/reports/operators?${qs(q)}`).then(normOperators),
  falseAlarms: (q: ReportQuery = {}) => req<unknown>(`/api/soc/reports/false-alarms?${qs(q)}`).then(normFalseAlarms),
  shifts: (limit = 60) => req<ShiftReport[] | { reports: ShiftReport[] }>(`/api/soc/reports/shifts?${qs({ limit })}`)
    .then((r) => (Array.isArray(r) ? r : r.reports ?? [])),
  shift: (id: number) => req<ShiftReport>(`/api/soc/reports/shifts/${encodeURIComponent(String(id))}`),
  /** Blank: the last completed shift (the hub's shift ends). */
  generateShift: (b: { start?: number; end?: number } = {}) => req<ShiftReport>("/api/soc/reports/shifts/generate", json("POST", b)),
  customer: (org: string, year: number, month: number) => req<CustomerSummary>(`/api/soc/reports/customers/${encodeURIComponent(org)}?${qs({ year, month })}`),
};
