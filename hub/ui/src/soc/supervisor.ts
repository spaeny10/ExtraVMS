/**
 * The supervisor view's rollups, pure (supervisor.test.ts). The hub's overview answers the same questions, but only
 * every 15 s; these read the live queue the SOC socket keeps, so a tile changes the moment a claim or an escalation
 * frame lands. The overview still supplies what the queue can't know (resolved in the last 24 h per operator).
 */
import { PRIORITY_RANK } from "./queue";
import type { Incident, OperatorLoad, Presence, PresenceStatus, Priority, SlaPolicy } from "./types";

const isActive = (i: Incident) => i.state === "new" || i.state === "claimed";

export type QueueHealth = {
  /** ringing lane, nobody has claimed it yet (what makes the alarm sound) */
  ringing: number;
  /** quiet lane, still open */
  quiet: number;
  /** age of the oldest unclaimed ringing incident (null = none) */
  oldestUnclaimedS: number | null;
  /** unclaimed past the claim deadline */
  breaches: number;
  /** claimed past the resolve deadline */
  overdue: number;
  byPriority: Record<Priority, number>;
  /** open incidents by escalation level (0 = not escalated) */
  byEscalation: Record<number, number>;
  /** awaiting a supervisor's verification */
  pendingVerify: number;
};

export function queueHealth(incidents: Incident[], now: number): QueueHealth {
  const h: QueueHealth = { ringing: 0, quiet: 0, oldestUnclaimedS: null, breaches: 0, overdue: 0, byPriority: { high: 0, medium: 0, low: 0 }, byEscalation: {}, pendingVerify: 0 };
  for (const i of incidents) {
    if (i.state === "pending_verify") { h.pendingVerify++; continue; }
    if (!isActive(i)) continue;
    if (i.priority in h.byPriority) h.byPriority[i.priority]++;
    const lvl = i.escalation_level ?? 0;
    h.byEscalation[lvl] = (h.byEscalation[lvl] ?? 0) + 1;
    if (i.lane === "quiet") { h.quiet++; continue; }
    if (i.state === "new") {
      h.ringing++;
      const age = now - i.opened_at;
      if (h.oldestUnclaimedS == null || age > h.oldestUnclaimedS) h.oldestUnclaimedS = Math.max(0, age);
      if (i.sla_due_at != null && i.sla_due_at < now) h.breaches++;
    } else if (i.resolve_due_at != null && i.resolve_due_at < now) h.overdue++;
  }
  return h;
}

export type BoardRow = {
  operator: OperatorLoad;
  status: PresenceStatus;
  /** what they are working: the incident their presence names, else the claim they have held longest */
  incident: Incident | null;
  /** seconds since they claimed it */
  onItS: number | null;
  /** past the incident's resolve SLA (the board shows the time in the breach tone) */
  overResolve: boolean;
  claimed: number;
  resolved24h: number;
  pendingVerify: number;
};

const STATUS_ORDER: Record<PresenceStatus, number> = { engaged: 0, available: 1, break: 2, offline: 3 };

/**
 * One row per SOC member, busiest first (engaged, available, on break, away), then by email. `presence` may be the
 * overview's operators (which carry claimed / pending_verify / resolved_24h) or the socket's roster; counts the
 * overview didn't send are worked out from the queue.
 */
export function operatorBoard(presence: (Presence | OperatorLoad)[], incidents: Incident[], now: number, sla?: SlaPolicy | null): BoardRow[] {
  const rows = presence.map((p): BoardRow => {
    const o = p as OperatorLoad;
    const held = incidents.filter((i) => i.state === "claimed" && i.claimed_by === p.user_id)
      .sort((a, b) => (a.claimed_at ?? 0) - (b.claimed_at ?? 0));
    const incident = (p.incident_id != null ? incidents.find((i) => i.id === p.incident_id && isActive(i)) : undefined) ?? held[0] ?? null;
    const since = incident?.claimed_at ?? null;
    const onItS = incident && incident.claimed_by === p.user_id && since != null ? Math.max(0, now - since) : null;
    const resolveS = incident ? sla?.[incident.priority]?.resolve_s ?? null : null;
    const overResolve = !!incident && ((incident.resolve_due_at != null && incident.resolve_due_at < now) || (onItS != null && resolveS != null && onItS > resolveS));
    const status: PresenceStatus = p.on_shift === false ? "offline" : p.status;
    return {
      operator: o, status, incident, onItS, overResolve,
      claimed: o.claimed ?? held.length,
      resolved24h: o.resolved_24h ?? 0,
      pendingVerify: o.pending_verify ?? incidents.filter((i) => i.state === "pending_verify" && i.resolved_by === p.user_id).length,
    };
  });
  return rows.sort((a, b) => STATUS_ORDER[a.status] - STATUS_ORDER[b.status] || a.operator.email.localeCompare(b.operator.email));
}

export type OpenFilters = { org: string | null; priority: Priority | null; state: "new" | "claimed" | "pending_verify" | null; escalation: number | null };
export const NO_OPEN_FILTERS: OpenFilters = { org: null, priority: null, state: null, escalation: null };

/**
 * Every open incident across customers (both lanes and those awaiting verification), filtered, most urgent first:
 * escalation level, then priority, then the oldest. `escalation` is a floor (≥ level).
 */
export function openIncidents(incidents: Incident[], f: OpenFilters): Incident[] {
  return incidents
    .filter((i) => i.state !== "closed" && (!f.org || i.org_id === f.org) && (!f.priority || i.priority === f.priority)
      && (!f.state || i.state === f.state) && (f.escalation == null || (i.escalation_level ?? 0) >= f.escalation))
    .sort((a, b) => (b.escalation_level ?? 0) - (a.escalation_level ?? 0) || (PRIORITY_RANK[a.priority] ?? 3) - (PRIORITY_RANK[b.priority] ?? 3)
      || a.opened_at - b.opened_at || a.id - b.id);
}

export type ResolvedRow = { incident: Incident; claimS: number | null; resolveS: number | null; canVerify: boolean; verifyBlocked: string | null };

/**
 * Today's resolved (closed or awaiting verification), resolved at or after `since`, newest first. Time to claim runs
 * from opening to the first claim; time to resolve from opening to the resolution (what the customer waited).
 * Verification is four-eyes: the resolver can't verify their own (the hub refuses too, with its own words).
 */
export function resolvedSince(list: Incident[], since: number, meId: string): ResolvedRow[] {
  return list
    .filter((i) => (i.state === "closed" || i.state === "pending_verify") && (i.resolved_at ?? i.closed_at ?? 0) >= since)
    .sort((a, b) => (b.resolved_at ?? b.closed_at ?? 0) - (a.resolved_at ?? a.closed_at ?? 0) || b.id - a.id)
    .map((i) => {
      const end = i.resolved_at ?? i.closed_at;
      const mine = i.resolved_by === meId;
      return {
        incident: i,
        claimS: i.first_claimed_at != null ? Math.max(0, i.first_claimed_at - i.opened_at) : null,
        resolveS: end != null ? Math.max(0, end - i.opened_at) : null,
        canVerify: i.state === "pending_verify" && !mine,
        verifyBlocked: i.state === "pending_verify" && mine ? "You resolved this one: a different supervisor has to verify it" : null,
      };
    });
}

/**
 * Which escalated incidents still need their chime on this page: level 2 or above, open, not chimed yet. The caller
 * records the ids it chimed so each incident sounds once, however many frames repeat its level.
 */
export function chimeDue(escalated: Pick<Incident, "id" | "escalation_level" | "state">[], chimed: ReadonlySet<number>): number[] {
  return escalated.filter((i) => (i.escalation_level ?? 0) >= 2 && (i.state === "new" || i.state === "claimed") && !chimed.has(i.id)).map((i) => i.id);
}

/** The customers present in a list of incidents, by name (the filter's options). */
export function customersOf(list: Pick<Incident, "org_id" | "org_name">[]): { id: string; name: string }[] {
  const m = new Map<string, string>();
  for (const i of list) m.set(i.org_id, i.org_name);
  return [...m.entries()].map(([id, name]) => ({ id, name })).sort((a, b) => a.name.localeCompare(b.name));
}

export type SlaDraft = Record<Priority, { claim: string; resolve: string; lane: "ring" | "quiet" }>;
export const PRIORITIES: Priority[] = ["high", "medium", "low"];

export function slaDraft(p: SlaPolicy): SlaDraft {
  const one = (r: SlaPolicy[Priority] | undefined) => ({ claim: r?.claim_s == null ? "" : String(r.claim_s), resolve: r?.resolve_s == null ? "" : String(r.resolve_s), lane: (r?.lane ?? (r?.ring === false ? "quiet" : "ring")) as "ring" | "quiet" });
  return { high: one(p.high), medium: one(p.medium), low: one(p.low) };
}

/**
 * The PUT body for the SLA editor: only the priorities and fields that changed (the hub merges partial rules), or an
 * error naming the bad field. Blank seconds = no clock (null), which the hub accepts.
 */
export function slaPatch(draft: SlaDraft, cur: SlaPolicy, parse: (v: string) => number | null | "invalid"): { patch: Partial<Record<Priority, Record<string, unknown>>> } | { error: string } {
  const patch: Partial<Record<Priority, Record<string, unknown>>> = {};
  for (const p of PRIORITIES) {
    const d = draft[p], c = cur[p];
    const claim = parse(d.claim), resolve = parse(d.resolve);
    if (claim === "invalid") return { error: `${p}: time to claim is whole seconds, 1 to 86400 (blank = no clock)` };
    if (resolve === "invalid") return { error: `${p}: time to resolve is whole seconds, 1 to 86400 (blank = no clock)` };
    const rule: Record<string, unknown> = {};
    if (claim !== (c?.claim_s ?? null)) rule.claim_s = claim;
    if (resolve !== (c?.resolve_s ?? null)) rule.resolve_s = resolve;
    const lane = c?.lane ?? (c?.ring === false ? "quiet" : "ring");
    if (d.lane !== lane) rule.lane = d.lane;
    if (Object.keys(rule).length) patch[p] = rule;
  }
  return { patch };
}

/**
 * The board's roster: presence from the socket (live), load counts from the overview (every 15 s). Someone the
 * overview lists but the socket roster doesn't yet (first seconds after opening) is kept as the overview has them.
 */
export function mergeRoster(live: Presence[], overview: OperatorLoad[] | null | undefined): OperatorLoad[] {
  const ov = new Map((overview ?? []).map((o) => [o.user_id, o]));
  const out: OperatorLoad[] = live.map((p) => { const o = ov.get(p.user_id); return o ? { ...o, ...p, claimed: o.claimed, pending_verify: o.pending_verify, resolved_24h: o.resolved_24h } : p; });
  for (const o of overview ?? []) if (!live.some((p) => p.user_id === o.user_id)) out.push(o);
  return out;
}

/**
 * The resolved list (REST, every 15 s) brought up to date with the live queue: an incident the queue holds as
 * awaiting verification is added or refreshed, and one the queue holds as new or claimed again (sent back by a
 * supervisor) leaves the list. Closed incidents leave the queue, so for those the REST row stands.
 */
export function withLive(resolved: Incident[], live: Incident[]): Incident[] {
  const m = new Map(resolved.map((i) => [i.id, i]));
  for (const i of live) {
    const o = m.get(i.id);
    if (i.state === "pending_verify") { if (!o || o.updated_at <= i.updated_at) m.set(i.id, i); }
    else if (isActive(i) && o && o.updated_at <= i.updated_at) m.delete(i.id);
  }
  return [...m.values()];
}
