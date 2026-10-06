/**
 * SLA timers, pure (sla.test.ts). Unclaimed incidents count down to the claim deadline (sla_due_at), claimed ones to
 * the resolve deadline (resolve_due_at). Tones are by the fraction of the window left so a 60 s high-priority claim
 * and a 20 min medium-priority resolve turn amber at the same point of their lives. Every tone has text too: color is
 * never the only signal (the pill's text and aria-label say it).
 */
import type { Incident, Priority, SlaPolicy } from "./types";

export type SlaTone = "ok" | "warn" | "urgent" | "breach";

/** ok > 50 % left, warn > 20 %, urgent > 0, breach at or past the deadline. A window of unknown length uses seconds. */
export function slaTone(remainingS: number, totalS: number | null | undefined): SlaTone {
  if (remainingS <= 0) return "breach";
  if (!totalS || totalS <= 0) return remainingS > 120 ? "ok" : remainingS > 30 ? "warn" : "urgent";
  const f = remainingS / totalS;
  return f > 0.5 ? "ok" : f > 0.2 ? "warn" : "urgent";
}

const pad = (n: number) => String(n).padStart(2, "0");
/** 252 → "4:12", 3852 → "1:04:12". */
export function clock(s: number): string {
  const t = Math.floor(Math.abs(s));
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), sec = t % 60;
  return h ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
}
/** "4:12 left" before the deadline, "+1:03 over" after it. */
export const slaText = (remainingS: number) => (remainingS > 0 ? `${clock(Math.ceil(remainingS))} left` : `+${clock(-remainingS)} over`);

export type SlaView = { kind: "claim" | "resolve"; due: number; remaining: number; tone: SlaTone; text: string; label: string };

/** The running timer for an incident now, or null when none applies (quiet lane, closed, no deadline). */
export function slaFor(i: Pick<Incident, "state" | "priority" | "sla_due_at" | "resolve_due_at" | "opened_at" | "claimed_at">, now: number, policy?: SlaPolicy | null): SlaView | null {
  const kind = i.state === "new" ? "claim" : i.state === "claimed" ? "resolve" : null;
  if (!kind) return null;
  const due = kind === "claim" ? i.sla_due_at : i.resolve_due_at;
  if (due == null) return null;
  const rule = policy?.[i.priority as Priority];
  // window length: the policy's, else the time from when the clock started (opening or claim) to the deadline
  const start = kind === "claim" ? i.opened_at : i.claimed_at ?? i.opened_at;
  const total = (kind === "claim" ? rule?.claim_s : rule?.resolve_s) ?? (due - start > 0 ? due - start : null);
  const remaining = due - now;
  const tone = slaTone(remaining, total);
  const text = slaText(remaining);
  const verb = kind === "claim" ? "to claim" : "to resolve";
  const label = remaining > 0 ? `${text.replace(" left", "")} left ${verb}` : `Past the deadline ${verb} by ${clock(-remaining)}`;
  return { kind, due, remaining, tone, text, label };
}

export const PRIORITY_LABEL: Record<string, string> = { high: "High", medium: "Medium", low: "Low" };
/** The site toolkit's threat colors (styles.css .badge.threat-*), so priority reads the same as on event cards. */
export const priorityClass = (p: string) => `badge threat-${p in PRIORITY_LABEL ? p : "none"}`;
export const priorityLabel = (p: string) => PRIORITY_LABEL[p] ?? p;
