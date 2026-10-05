/** Text for incident log rows and queue rows, pure (format.test.ts), shared by the workstation, phone and customer views. */
import type { ProcedureStep, SiteContact } from "../api";
import { CALL_OUTCOMES, type Incident, type IncidentProcedure, type LogRow, type SopProgress } from "./types";

const str = (v: unknown) => (typeof v === "string" ? v : typeof v === "number" ? String(v) : "");
const OUTCOME = Object.fromEntries(CALL_OUTCOMES.map((o) => [o.id, o.label]));
const human = (code: string) => code.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());

/**
 * One line per log row. The hub writes `detail` as an object; fields it may carry are read when present and the
 * line falls back to the bare action, so a newer hub's extra actions still show up as something readable.
 */
export function logText(r: Pick<LogRow, "action" | "detail">, contacts: Pick<SiteContact, "id" | "name">[] = [], dispositionLabel: (code: string) => string = human): string {
  const d = (r.detail && typeof r.detail === "object" ? r.detail : {}) as Record<string, unknown>;
  const text = typeof r.detail === "string" ? r.detail : str(d.text ?? d.notes ?? d.note);
  switch (r.action) {
    case "opened": return `Opened${d.priority ? ` (${str(d.priority)} priority)` : ""}`;
    case "event_added": return `Another event${d.camera_name ? ` on ${str(d.camera_name)}` : ""}${d.priority ? ` (${str(d.priority)})` : ""}`;
    case "claimed": case "claim": return `Claimed${typeof d.after_s === "number" ? ` after ${clockText(d.after_s)}` : ""}`;
    case "released": case "release": return "Released back to the queue";
    case "takeover": return `Taken over${d.from_email ? ` from ${str(d.from_email)}` : ""}`;
    case "handoff": return `Handed off${d.to_email ? ` to ${str(d.to_email)}` : ""}`;
    case "promoted": case "promote": return "Moved to the ringing lane";
    case "priority": case "priority_raised": return `Priority raised to ${str(d.to ?? d.priority)}${d.from ? ` (was ${str(d.from)})` : ""}`;
    case "escalated": return `Escalated to level ${str(d.level)}${text ? `: ${text}` : ""}`;
    case "note": return `Note: ${text}`;
    case "call": case "calls": {
      const who = str(d.name) || str(d.contact_name) || contacts.find((c) => c.id === Number(d.contact_id))?.name || "contact";
      return `Called ${who}: ${OUTCOME[str(d.outcome)] ?? human(str(d.outcome) || "logged")}${text ? ` · ${text}` : ""}`;
    }
    case "sop": {
      // the step's own text is in `text`, so only `note` is a remark here
      const step = (typeof r.detail === "object" ? str(d.text) || str(d.step_text) : "") || `step ${str(d.step_id)}`;
      const title = str(d.title) || str(d.procedure_title);
      return `${d.done === false ? "Unticked" : "Ticked"} ${step}${title ? ` (${title})` : ""}${d.note ? ` · ${str(d.note)}` : ""}`;
    }
    case "relay": {
      const cam = str(d.camera_name) || str(d.camera_id);
      return `Relay switched ${d.on ? "on" : "off"}${cam ? ` (${cam})` : ""}${d.ok === false ? `, failed${d.error ? `: ${str(d.error)}` : ""}` : ""}`;
    }
    case "resolved": case "resolve": return `Resolved: ${dispositionLabel(str(d.disposition))}${text ? ` · ${text}` : ""}${d.four_eyes || d.pending_verify ? " (awaiting supervisor verification)" : ""}`;
    case "verified": case "verify": return "Verified by a supervisor";
    case "rejected": case "reject": return `Sent back by a supervisor${text ? `: ${text}` : ""}`;
    case "swept": case "sweep": return "Swept from the quiet lane";
    case "expired": return "Expired unhandled";
    case "feedback": return `False-alarm feedback sent to the site${Array.isArray(d.failed) && d.failed.length ? ` (${d.failed.length} not delivered yet)` : ""}`;
    default: return `${human(r.action)}${text ? `: ${text}` : ""}`;
  }
}

const clockText = (s: number) => (s < 90 ? `${Math.round(s)} s` : `${Math.round(s / 60)} min`);

/** "3 min" / "45 s" / "2 h": how long ago, short enough for a queue row. */
export function age(ts: number, now: number): string {
  const s = Math.max(0, Math.round(now - ts));
  return s < 90 ? `${s} s` : s < 5400 ? `${Math.round(s / 60)} min` : s < 172800 ? `${Math.round(s / 3600)} h` : `${Math.round(s / 86400)} d`;
}

/** Distinct camera names of an incident, in event order (queue rows carry `cameras`, the detail carries events). */
export function cameraNames(i: Pick<Incident, "events" | "cameras">): string[] {
  const names = i.events?.length ? i.events.map((e) => e.camera_name || e.camera_id) : (i.cameras ?? []).map((c) => c.name || c.camera_id);
  return [...new Set(names)];
}

/** What the incident is about, for a row: the title, else the first event's kind. */
export const incidentTitle = (i: Pick<Incident, "title" | "events" | "id">) => i.title || (i.events?.[0]?.kind ? human(i.events[0].kind) : `Incident #${i.id}`);

/** An incident event's id as the site API wants it (the hub sends it as a string). */
export const eventNum = (e: { event_id: string | number }) => Number(e.event_id);

/** "Acme › HQ". */
export const whereText = (i: Pick<Incident, "org_name" | "location_name">) => `${i.org_name} › ${i.location_name}`;

export const STATE_LABEL: Record<string, string> = { new: "Unclaimed", claimed: "Claimed", pending_verify: "Awaiting verification", closed: "Closed" };

// ---- Respond pane: which procedures apply, their steps numbered for the 1–9 keys, who has been called

const RANK: Record<string, number> = { low: 0, medium: 1, high: 2 };
/** A procedure applies from its priority up (null = every incident). */
export const applicableProcedures = <P extends Pick<IncidentProcedure, "priority" | "order">>(procs: P[], priority: string) =>
  [...procs].filter((p) => !p.priority || (RANK[priority] ?? 0) >= (RANK[p.priority] ?? 0)).sort((a, b) => a.order - b.order);

export const procStepId = (s: ProcedureStep, n: number) => s.id ?? `s${n + 1}`;

export type FlatStep = { procedureId: number; stepId: string; text: string; required: boolean; done: boolean; by: string | null; n: number };
/**
 * Every applicable step in display order, numbered from 1 (the digit that ticks it, up to 9). The hub already sends
 * only applicable procedures with each step's state on it; a separate progress map (older contract) is read too.
 */
export function flatSteps(procs: IncidentProcedure[], priority: string, progress: SopProgress = {}): FlatStep[] {
  let n = 0;
  return applicableProcedures(procs, priority).flatMap((p) => p.steps.map((s, i) => {
    const id = procStepId(s, i);
    const tick = s.done !== undefined ? { done: s.done, by: s.by ?? null } : progress[String(p.id)]?.[id];
    return { procedureId: p.id ?? 0, stepId: id, text: s.text, required: s.required, done: !!tick?.done, by: tick?.by ?? null, n: ++n };
  }));
}

/** Logged call outcomes per contact id, oldest first. */
export function callsByContact(log: LogRow[]): Map<number, LogRow[]> {
  const m = new Map<number, LogRow[]>();
  for (const r of [...log].sort((a, b) => a.ts - b.ts)) {
    if (r.action !== "call" && r.action !== "calls") continue;
    const id = Number((r.detail as Record<string, unknown> | null)?.contact_id);
    if (!Number.isFinite(id)) continue;
    m.set(id, [...(m.get(id) ?? []), r]);
  }
  return m;
}

/** The next contact to call: the first in call order nobody has logged a call to yet (null = everyone called). */
export function nextUncalled<C extends Pick<SiteContact, "id" | "order">>(contacts: C[], log: LogRow[]): C | null {
  const called = callsByContact(log);
  return [...contacts].sort((a, b) => a.order - b.order).find((c) => c.id != null && !called.has(c.id)) ?? null;
}

// ---- reports and the supervisor view: durations, rates, CSV, time ranges

/**
 * A duration for a report cell: "45 s", "3 min", "1 h 20 min", "2 d 3 h"; "—" when there is no figure (an operator
 * with nothing resolved has no p50, which is not the same as 0 s).
 */
export function fmtDur(s: number | null | undefined): string {
  if (s == null || !Number.isFinite(s)) return "—";
  const t = Math.max(0, Math.round(s));
  if (t < 90) return `${t} s`;
  if (t < 3600) return `${Math.round(t / 60)} min`;
  if (t < 86400) { const h = Math.floor(t / 3600), m = Math.round((t % 3600) / 60); return m === 60 ? `${h + 1} h` : m ? `${h} h ${m} min` : `${h} h`; }
  const d = Math.floor(t / 86400), h = Math.round((t % 86400) / 3600);
  return h === 24 ? `${d + 1} d` : h ? `${d} d ${h} h` : `${d} d`;
}

/** A 0..1 rate as "12%" (one decimal under 10 %, where the difference between 0.4 % and 1 % matters); "—" for none. */
export function percent(rate: number | null | undefined): string {
  if (rate == null || !Number.isFinite(rate)) return "—";
  const p = Math.max(0, rate) * 100;
  return p > 0 && p < 10 ? `${(Math.round(p * 10) / 10).toString()}%` : `${Math.round(p)}%`;
}

/** part of whole as a 0..1 rate, null when there is no whole (0 of 0 is not 0 %). */
export const rateOf = (part: number, whole: number): number | null => (whole > 0 ? part / whole : null);

type Cell = string | number | boolean | null | undefined;
/**
 * RFC 4180 CSV (CRLF rows, fields quoted when they hold a comma, quote or line break). Text that a spreadsheet would
 * run as a formula (= + - @ at the start: a Site or camera name is customer-typed) gets a leading apostrophe, so
 * opening the export can never execute anything. Numbers are written as they are, negative ones included.
 */
export function toCsv(rows: Cell[][]): string {
  const cell = (v: Cell) => {
    if (v == null) return "";
    if (typeof v === "number") return Number.isFinite(v) ? String(v) : "";
    let s = String(v);
    if (/^[=+\-@\t\r]/.test(s)) s = `'${s}`;
    return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  return rows.map((r) => r.map(cell).join(",")).join("\r\n") + "\r\n";
}

export const SHIFT_HOURS = [6, 14, 22] as const;
/**
 * When the current shift began, in the browser's local time. The handovers are the hub's default shift ends
 * (settings.soc_shift_ends "06:00,14:00,22:00", in the hub's own time zone); the hub doesn't send them, so this is
 * only the window for "this shift" and "resolved this shift" here, never a figure in a stored report.
 */
export function shiftStart(now: number, hours: readonly number[] = SHIFT_HOURS): number {
  const d = new Date(now * 1000);
  const sorted = [...hours].sort((a, b) => b - a);
  for (const h of sorted) {
    const t = new Date(d.getFullYear(), d.getMonth(), d.getDate(), h).getTime() / 1000;
    if (t <= now) return t;
  }
  // before the first handover today: the last one yesterday
  return new Date(d.getFullYear(), d.getMonth(), d.getDate() - 1, sorted[0]).getTime() / 1000;
}

export const RANGES = [["shift", "This shift"], ["24h", "24 h"], ["7d", "7 d"], ["30d", "30 d"], ["custom", "Custom"]] as const;
export type RangeChoice = (typeof RANGES)[number][0];
export type Range = { since: number; until: number };

/** The report window for a choice; custom takes the picked bounds (swapped when entered backwards). */
export function reportRange(choice: RangeChoice, now: number, custom?: Partial<Range> | null): Range {
  const until = Math.floor(now);
  switch (choice) {
    case "shift": return { since: Math.floor(shiftStart(now)), until };
    case "24h": return { since: until - 86400, until };
    case "7d": return { since: until - 7 * 86400, until };
    case "30d": return { since: until - 30 * 86400, until };
    case "custom": {
      const a = custom?.since ?? until - 86400, b = custom?.until ?? until;
      return a <= b ? { since: a, until: b } : { since: b, until: a };
    }
  }
}

/** A <input type="datetime-local"> value for an epoch time (local), and back (NaN-safe: null for an empty field). */
export const toLocalInput = (ts: number) => {
  const d = new Date(ts * 1000), p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
};
export const fromLocalInput = (v: string): number | null => { if (!v) return null; const t = new Date(v).getTime(); return Number.isFinite(t) ? Math.floor(t / 1000) : null; };

/** "Seconds" input for the SLA editor: blank = no clock (null), otherwise a whole positive number of seconds. */
export function parseSeconds(v: string): number | null | "invalid" {
  const s = v.trim();
  if (!s) return null;
  if (!/^\d+$/.test(s)) return "invalid";
  const n = Number(s);
  return n >= 1 && n <= 86400 ? n : "invalid";
}

/** "false_alarms" / "p50ClaimS" → "False alarms" / "P50 claim s": a readable label for a key the hub chose. */
export const keyLabel = (k: string) => k.replace(/([a-z])([A-Z])/g, "$1 $2").replace(/[_-]+/g, " ").trim().toLowerCase().replace(/^./, (c) => c.toUpperCase());

/** Keys that hold times or periods, not counts: never a tile. */
const NOT_A_COUNT = /^(start|end|since|until|year|month|period_start|period_end|created_at|n)$|_at$|_ts$/;

/** A report number as text: keys ending in _s are durations, rates and shares are 0..1, the rest are counts. */
export function reportValue(k: string, v: number): string {
  if (/_s$/.test(k)) return fmtDur(v);
  if (/(^|_)(rate|share|coverage)$/.test(k)) return percent(v);
  return (Math.round(v * 10) / 10).toLocaleString();
}

/**
 * The numbers of a report's `data` as tiles: its top-level numbers, then those inside the summary objects the hub
 * nests them in (`totals` for a customer month, `counts` and `calls` for a shift). The hub may add fields: whatever
 * is a number shows up, in the order sent; times and periods are left out. A nested `total` takes its parent's
 * name ("Calls").
 */
export function numberTiles(data: Record<string, unknown> | null | undefined, nested: readonly string[] = ["totals", "counts", "calls"]): { key: string; label: string; value: string }[] {
  if (!data) return [];
  const out: { key: string; label: string; value: string }[] = [];
  const add = (k: string, v: unknown, key: string, label: string) => {
    if (typeof v !== "number" || !Number.isFinite(v) || NOT_A_COUNT.test(k) || out.some((t) => t.label === label)) return;
    out.push({ key, label, value: reportValue(k, v) });
  };
  for (const [k, v] of Object.entries(data)) add(k, v, k, keyLabel(k.replace(/_s$/, "")));
  for (const parent of nested) {
    const o = data[parent];
    if (!o || typeof o !== "object" || Array.isArray(o)) continue;
    for (const [k, v] of Object.entries(o)) add(k, v, `${parent}.${k}`, k === "total" ? keyLabel(parent) : keyLabel(k.replace(/_s$/, "")));
  }
  return out;
}
