/**
 * Wire types of the SOC API (hub/hub/soc_api.py, soc.py). Times are epoch seconds. An incident groups the verified
 * events of one Site during armed hours; `lane` ring = must be claimed within the SLA (alarm sound), quiet = low
 * priority, swept in bulk.
 */
import type { Procedure, ProcedureStep, SiteContact, SocRole } from "../api";
import type { AddressParts, Place } from "../place";

export type Priority = "high" | "medium" | "low";
export type IncidentState = "new" | "claimed" | "pending_verify" | "closed";
export type Lane = "ring" | "quiet";

/**
 * One site event in an incident. `event_id` is the event's id on its server, sent as a string (the hub's column is
 * text; format.eventNum turns it into the number the site API takes). `detail` is what the hub copied at ingest
 * (label, synopsis, policy, watched, start_ts), loosely typed.
 */
export type IncidentEvent = {
  server_id: string; server_name: string; event_id: string | number; camera_id: string; camera_name: string;
  priority: Priority | string; kind: string; ts: number; detail: Record<string, unknown> | null;
};

export type Incident = {
  id: number; org_id: string; org_name: string; location_id: string; location_name: string;
  opened_at: number; last_event_at: number; updated_at: number; closed_at: number | null;
  state: IncidentState; priority: Priority; lane: Lane;
  claimed_by: string | null; claimed_by_email: string | null; claimed_at: number | null; first_claimed_at: number | null; assigned_by: string | null;
  sla_due_at: number | null; resolve_due_at: number | null; escalation_level: number;
  disposition: string | null; disposition_notes: string | null; resolved_by: string | null; resolved_at: number | null; four_eyes_by: string | null;
  event_count: number; title: string | null;
  /** soc.tag: the servers and cameras involved (queue rows carry these, not the events; the detail has the events) */
  servers?: { id: string; name: string }[];
  cameras?: { server_id: string; camera_id: string; name: string }[];
  location_timezone?: string | null;
  /** only some payloads embed events (the detail lists them apart); kept optional so either works */
  events?: IncidentEvent[];
  /** the customer-side list (GET /api/locations/{id}/incidents) embeds the log */
  log?: LogRow[];
};

/**
 * Append-only incident log. `action` as the hub writes it: opened, event_added, priority_raised, claim, release,
 * takeover, handoff, note, call, sop, relay, promote, resolve, verify, swept, feedback. The customer-side list sends
 * `by` ("SOC" for SOC staff who aren't members of the customer) instead of user_id/user_email.
 */
export type LogRow = { id: number; ts: number; user_id?: string | null; user_email?: string | null; by?: string | null; action: string; detail: Record<string, unknown> | string | null };

/** SOP ticks derived from the log: procedure id → step id → last tick (only when a hub sends it apart). */
export type SopProgress = Record<string, Record<string, { done: boolean; by: string | null; at: number }>>;

/**
 * The hub sends only the procedures that apply at the incident's priority, each step carrying its state from the log
 * (done / by / at / note), plus done_count and complete.
 */
export type ProgressStep = ProcedureStep & { done?: boolean; by?: string | null; at?: number | null; note?: string | null };
export type IncidentProcedure = Omit<Procedure, "steps"> & { steps: ProgressStep[]; done_count?: number; complete?: boolean };

export type IncidentDetail = {
  incident: Incident; events: IncidentEvent[]; log: LogRow[];
  /** where the Site is, for the Dispatch block (newer hubs) */
  site?: Place & { id: string };
  contacts: SiteContact[]; procedures: IncidentProcedure[];
  /** older shape of the contract: progress apart from the procedures */
  sop_progress?: SopProgress;
  calls?: LogRow[];
};

export type CallOutcome = "spoke" | "voicemail" | "no_answer" | "busy" | "dispatched" | "refused";
export const CALL_OUTCOMES: { id: CallOutcome; label: string }[] = [
  { id: "spoke", label: "Spoke" }, { id: "voicemail", label: "Voicemail" }, { id: "no_answer", label: "No answer" },
  { id: "busy", label: "Busy" }, { id: "dispatched", label: "Dispatched" }, { id: "refused", label: "Refused" },
];

/** The disposition catalog (GET /api/soc/dispositions): three groups, each with a chord leader and digits. */
export type Disposition = { code: string; label: string; needs_notes: boolean; selectable: boolean; four_eyes: Priority[]; key: string | null };
export type DispositionGroup = { id: "true_alarm" | "false_alarm" | "not_actionable" | string; label: string; key: string; dispositions: Disposition[] };
export type Dispositions = { groups: DispositionGroup[] };

export type PresenceStatus = "available" | "engaged" | "break" | "offline";
export type Presence = {
  user_id: string; email: string; soc_role: SocRole | null; status: PresenceStatus; since: number | null; incident_id: number | null;
  last_seen_at?: number | null; on_shift?: boolean;
};

/** GET /api/soc/sla answers {sla, defaults}; each rule says which lane the priority goes to. */
export type SlaRule = { claim_s: number | null; resolve_s: number | null; lane?: Lane; ring?: boolean };
export type SlaPolicy = Record<Priority, SlaRule>;

export type SocSite = {
  id: string; name: string; org_id: string; org_name: string | null; timezone: string | null; monitored: boolean; armed: boolean; reason: string;
  next_change: { at: number; armed: boolean } | null; override: unknown | null; open_incidents: number; ringing: number;
  servers_total: number; servers_online: number;
  /** newer hubs: where the Site is */
  address?: string; lat?: number | null; lon?: number | null; address_parts?: AddressParts | null;
};

/** The server's sound policy, on every frame: ring (how often, for which top priority) or stay quiet. */
export type SoundPolicy = { ring: boolean; repeat_s?: number | null; priority?: Priority | null };

export type SocMessage =
  | { type: "snapshot"; incidents: Incident[]; presence: Presence[]; ring_count?: number; sound?: SoundPolicy }
  /** opened / event_added also carry the `event` that caused them */
  | { type: "incident_opened" | "incident_updated" | "incident_event_added" | "incident_resolved" | "incident_escalated"; incident: Incident; event?: IncidentEvent; sound?: SoundPolicy; ring_count?: number }
  /** the hub sends one changed entry; a whole roster is accepted too */
  | { type: "presence"; presence: Presence | Presence[]; sound?: SoundPolicy; ring_count?: number }
  | { type: "arming"; location_id: string; armed: boolean; reason: string; site?: SocSite; sound?: SoundPolicy; ring_count?: number };

// ---- supervisor and reports (soc_api.py overview, soc_reports.py)

/** A roster entry as the overview sends it: presence plus the operator's load. */
export type OperatorLoad = Presence & { claimed?: number; pending_verify?: number; resolved_24h?: number };

/**
 * GET /api/soc/overview. `escalations` and `overdue` arrive with the escalation engine (stage 3); the page reads them
 * when present and works them out from the queue otherwise.
 */
export type Overview = {
  now: number; by_state: Record<string, number>; by_lane: Record<string, number>; by_escalation: Record<string, number>;
  breaches: { claim: number; resolve: number }; oldest_unclaimed_at: number | null; ring_count: number; sound?: SoundPolicy;
  operators: OperatorLoad[];
  escalations?: { level1?: number; level2?: number; level3?: number };
  overdue?: number;
};

export type OperatorStat = {
  user_id: string; email: string; claimed: number; resolved: number;
  p50_claim_s: number | null; p95_claim_s: number | null; p50_resolve_s: number | null; p95_resolve_s: number | null;
  dispositions: Record<string, number>;
  /** the hub's escalations_received: escalated / overdue while they held the incident */
  escalations: number;
  /** incidents they picked up after these had escalated */
  escalated_claimed?: number;
  false_alarm_share: number | null;
};

/**
 * `rate` is false alarms over judged incidents (closed minus swept / expired ones nobody looked at); `last_ts` is
 * the opening time of the newest closed incident (the hub's last_incident_at).
 */
export type FalseAlarmSite = { location_id: string; name: string; org_name: string | null; closed: number; judged: number; false: number; rate: number | null; top_disposition: string | null; last_ts: number | null };
export type FalseAlarmCamera = {
  location_id: string; server_id: string; server_name: string | null; camera_id: string; camera_name: string | null;
  closed: number; judged: number; false: number; rate: number | null; top_disposition: string | null; last_ts: number | null;
};
export type FalseAlarms = { sites: FalseAlarmSite[]; cameras: FalseAlarmCamera[] };

/** A stored shift report. `data` holds the numbers the text was written from (loosely typed: the hub may add more). */
export type ShiftReport = { id: number; period_start: number; period_end: number; created_at: number; text: string | null; data: Record<string, unknown> | null; model: string | null };

export type CustomerSummarySite = Record<string, unknown> & { location_id?: string; id?: string; name?: string };
/**
 * GET /api/soc/reports/customers/{org}: the stored monthly report row (the hub builds and stores it on first ask).
 * `data` carries org_id, year, month, sites (per-Site numbers and breakdowns) and totals.
 */
export type CustomerSummary = {
  id?: number; org_id?: string; period_start?: number; period_end?: number; created_at?: number; model?: string | null; text: string | null;
  data: (Record<string, unknown> & { year?: number; month?: number; sites?: CustomerSummarySite[]; totals?: Record<string, unknown> }) | null;
};
