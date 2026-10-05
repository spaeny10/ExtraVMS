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
