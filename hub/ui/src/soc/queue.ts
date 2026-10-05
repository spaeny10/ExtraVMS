/**
 * The operator queue, pure (queue.test.ts): which lane an incident sits in, the order an operator should work them,
 * the filter chips, and how a socket frame changes the queue. The hub decides lanes and SLAs; the UI only orders and
 * filters what it was sent.
 */
import type { Incident, Presence, Priority, SocMessage } from "./types";

export const PRIORITY_RANK: Record<string, number> = { high: 0, medium: 1, low: 2 };
const rank = (p: string) => PRIORITY_RANK[p] ?? 3;
const isOpen = (i: Incident) => i.state === "new" || i.state === "claimed";

/**
 * Open incidents by lane; resolved-awaiting-verification ones are listed apart (`verify`): they are no longer an
 * operator's work, but a supervisor needs to see them.
 */
export function splitLanes(list: Incident[]): { ring: Incident[]; quiet: Incident[]; verify: Incident[] } {
  const ring: Incident[] = [], quiet: Incident[] = [], verify: Incident[] = [];
  for (const i of list) {
    if (i.state === "pending_verify") verify.push(i);
    else if (!isOpen(i)) continue;
    else if (i.lane === "ring") ring.push(i);
    else quiet.push(i);
  }
  return { ring: orderRinging(ring), quiet: orderQuiet(quiet), verify };
}

/** Ringing lane: highest priority first, then the claim (or resolve) deadline nearest first, then the oldest. */
export function orderRinging(list: Incident[]): Incident[] {
  const due = (i: Incident) => (i.state === "new" ? i.sla_due_at : i.resolve_due_at ?? i.sla_due_at) ?? Infinity;
  return [...list].sort((a, b) => rank(a.priority) - rank(b.priority) || due(a) - due(b) || a.opened_at - b.opened_at || a.id - b.id);
}

/** Quiet lane: newest activity first, the order a sweep reads a grid of stills in. */
export function orderQuiet(list: Incident[]): Incident[] {
  return [...list].sort((a, b) => b.last_event_at - a.last_event_at || b.id - a.id);
}

export type QueueFilters = { org: string | null; priority: Priority | null; mine: boolean; unclaimed: boolean; text: string };
export const NO_FILTERS: QueueFilters = { org: null, priority: null, mine: false, unclaimed: false, text: "" };

/** The text box matches customer, Site, title, kind and camera names (case-insensitive, every word must match). */
export function matchesText(i: Incident, text: string): boolean {
  const words = text.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return true;
  const hay = [i.org_name, i.location_name, i.title, i.claimed_by_email, `#${i.id}`, ...(i.events ?? []).flatMap((e) => [e.camera_name, e.kind, e.server_name]),
    ...(i.cameras ?? []).map((c) => c.name), ...(i.servers ?? []).map((x) => x.name)]
    .filter(Boolean).join(" ").toLowerCase();
  return words.every((w) => hay.includes(w));
}

export function applyFilters(list: Incident[], f: QueueFilters, meId: string): Incident[] {
  return list.filter((i) => (!f.org || i.org_id === f.org) && (!f.priority || i.priority === f.priority)
    && (!f.mine || i.claimed_by === meId) && (!f.unclaimed || !i.claimed_by) && matchesText(i, f.text));
}

/** What makes the alarm sound: a ringing-lane incident nobody has claimed yet. */
export const isRingingUnclaimed = (i: Incident) => i.lane === "ring" && i.state === "new" && !i.claimed_by;

/** The priority the ringer should play (the most urgent unclaimed ringing incident), or null for silence. */
export function ringPriority(list: Incident[]): Priority | null {
  let best: Priority | null = null;
  for (const i of list) if (isRingingUnclaimed(i) && (best === null || rank(i.priority) < rank(best))) best = i.priority;
  return best;
}

/** Auto-advance after a resolve: the first unclaimed ringing incident in queue order other than `except`. */
export function nextRinging(list: Incident[], except?: number | null): Incident | null {
  return orderRinging(list.filter((i) => isRingingUnclaimed(i) && i.id !== except))[0] ?? null;
}

/** j/k over the visible rows: the id `step` rows away from `current` (clamped; no current = the first row). */
export function stepId(ids: number[], current: number | null, step: 1 | -1): number | null {
  if (!ids.length) return null;
  const at = current == null ? -1 : ids.indexOf(current);
  if (at < 0) return ids[0];
  return ids[Math.max(0, Math.min(ids.length - 1, at + step))];
}

// ---- the queue as the socket keeps it

export type QueueState = {
  incidents: Incident[]; presence: Presence[]; ringCount: number;
  /** arming changes seen on the socket (location id → armed), for the Sites the console shows */
  arming: Record<string, { armed: boolean; reason: string }>;
  /** bumped on every frame that changed an incident, so views holding detail know to refetch */
  rev: number;
};
export const EMPTY_QUEUE: QueueState = { incidents: [], presence: [], ringCount: 0, arming: {}, rev: 0 };

/**
 * Put one incident row in the queue. Closed incidents leave it. An older row (frames can cross a REST refetch) never
 * overwrites a newer one.
 */
export function upsertIncident(list: Incident[], inc: Incident): Incident[] {
  const at = list.findIndex((x) => x.id === inc.id);
  const old = at >= 0 ? list[at] : null;
  if (old && old.updated_at > inc.updated_at) return list;
  // a row without its events (some frames are slim) keeps the events we already had
  const merged = old && !inc.events && old.events ? { ...inc, events: old.events } : inc;
  if (merged.state === "closed") return at >= 0 ? list.filter((x) => x.id !== inc.id) : list;
  if (at < 0) return [...list, merged];
  const next = list.slice();
  next[at] = merged;
  return next;
}

export function applyStreamMessage(s: QueueState, m: SocMessage): QueueState {
  switch (m.type) {
    case "snapshot":
      return { ...s, incidents: m.incidents.filter((i) => i.state !== "closed"), presence: m.presence ?? s.presence, ringCount: m.ring_count ?? s.ringCount, rev: s.rev + 1 };
    case "presence":
      return { ...s, presence: mergePresence(s.presence, m.presence) };
    case "arming":
      return { ...s, arming: { ...s.arming, [m.location_id]: { armed: m.armed, reason: m.reason } } };
    case "incident_opened": case "incident_updated": case "incident_event_added": case "incident_resolved": case "incident_escalated": {
      // a resolve that needs four-eyes comes back as pending_verify (it stays, in `verify`); closed ones leave
      return { ...s, incidents: upsertIncident(s.incidents, m.incident), ringCount: m.ring_count ?? s.ringCount, rev: s.rev + 1 };
    }
    default:
      return s;
  }
}

/** A presence frame carries one changed entry (the hub's set_presence) or, from older code, the whole roster. */
export function mergePresence(list: Presence[], p: Presence | Presence[]): Presence[] {
  if (Array.isArray(p)) return p;
  if (!p?.user_id) return list;
  const at = list.findIndex((x) => x.user_id === p.user_id);
  if (at < 0) return [...list, p].sort((a, b) => a.email.localeCompare(b.email));
  const next = list.slice();
  next[at] = p;
  return next;
}

/** Counts for the queue header. */
export function queueCounts(list: Incident[], meId: string): { ringing: number; unclaimed: number; quiet: number; mine: number } {
  const { ring, quiet } = splitLanes(list);
  return { ringing: ring.length, unclaimed: ring.filter((i) => !i.claimed_by).length, quiet: quiet.length, mine: list.filter((i) => isOpen(i) && i.claimed_by === meId).length };
}
