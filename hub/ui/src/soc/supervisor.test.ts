import { describe, expect, it } from "vitest";
import { NO_OPEN_FILTERS, chimeDue, customersOf, mergeRoster, openIncidents, operatorBoard, queueHealth, resolvedSince, slaDraft, slaPatch, withLive } from "./supervisor";
import { parseSeconds } from "./format";
import type { Incident, Presence, SlaPolicy } from "./types";

let seq = 0;
function inc(p: Partial<Incident> = {}): Incident {
  return {
    id: p.id ?? ++seq, org_id: "o1", org_name: "Acme", location_id: "l1", location_name: "HQ", opened_at: 1000, last_event_at: 1000, updated_at: 1000, closed_at: null,
    state: "new", priority: "medium", lane: "ring", claimed_by: null, claimed_by_email: null, claimed_at: null, first_claimed_at: null, assigned_by: null,
    sla_due_at: 1180, resolve_due_at: null, escalation_level: 0, disposition: null, disposition_notes: null, resolved_by: null, resolved_at: null, four_eyes_by: null,
    event_count: 1, title: null, ...p,
  };
}
const pres = (p: Partial<Presence> & { user_id: string }): Presence => ({ email: `${p.user_id}@x`, soc_role: "operator", status: "available", since: 0, incident_id: null, on_shift: true, ...p });
const POLICY: SlaPolicy = { high: { claim_s: 60, resolve_s: 600, lane: "ring" }, medium: { claim_s: 180, resolve_s: 1200, lane: "ring" }, low: { claim_s: null, resolve_s: null, lane: "quiet" } };

describe("queueHealth", () => {
  it("counts ringing, quiet, breaches, overdue and the oldest unclaimed", () => {
    const h = queueHealth([
      inc({ id: 1, opened_at: 900, sla_due_at: 960 }),                       // unclaimed, past its claim deadline
      inc({ id: 2, opened_at: 1100, sla_due_at: 1280, priority: "high" }),   // unclaimed, in time
      inc({ id: 3, state: "claimed", claimed_by: "a", resolve_due_at: 1150, escalation_level: 2 }), // overdue
      inc({ id: 4, lane: "quiet", priority: "low", sla_due_at: null }),
      inc({ id: 5, state: "pending_verify" }),
      inc({ id: 6, state: "closed" }),
    ], 1200);
    expect(h.ringing).toBe(2);
    expect(h.quiet).toBe(1);
    expect(h.breaches).toBe(1);
    expect(h.overdue).toBe(1);
    expect(h.oldestUnclaimedS).toBe(300);
    expect(h.pendingVerify).toBe(1);
    expect(h.byPriority).toEqual({ high: 1, medium: 2, low: 1 });
    expect(h.byEscalation).toEqual({ 0: 3, 2: 1 });
  });
  it("nothing open", () => {
    expect(queueHealth([], 5).oldestUnclaimedS).toBe(null);
  });
});

describe("operatorBoard", () => {
  it("finds what each operator works and for how long", () => {
    const list = [inc({ id: 7, state: "claimed", claimed_by: "a", claimed_at: 1000, priority: "high", resolve_due_at: 1600 }),
      inc({ id: 8, state: "claimed", claimed_by: "a", claimed_at: 1100 })];
    const rows = operatorBoard([pres({ user_id: "b", status: "break" }), pres({ user_id: "a", status: "engaged" }), pres({ user_id: "c", on_shift: false })], list, 1700, POLICY);
    expect(rows.map((r) => r.operator.user_id)).toEqual(["a", "b", "c"]);
    expect(rows[0].incident?.id).toBe(7);   // the longest-held claim
    expect(rows[0].onItS).toBe(700);
    expect(rows[0].overResolve).toBe(true);
    expect(rows[0].claimed).toBe(2);
    expect(rows[2].status).toBe("offline");  // aged out of the roster counts as away
    expect(rows[1].incident).toBe(null);
  });
  it("prefers the incident presence names and the overview's counts", () => {
    const list = [inc({ id: 7, state: "claimed", claimed_by: "a", claimed_at: 1000 }), inc({ id: 8, state: "claimed", claimed_by: "a", claimed_at: 1100 })];
    const [r] = operatorBoard([{ ...pres({ user_id: "a", status: "engaged", incident_id: 8 }), claimed: 5, resolved_24h: 9, pending_verify: 1 }], list, 1200, POLICY);
    expect(r.incident?.id).toBe(8);
    expect(r.onItS).toBe(100);
    expect(r.overResolve).toBe(false);
    expect([r.claimed, r.resolved24h, r.pendingVerify]).toEqual([5, 9, 1]);
  });
});

describe("mergeRoster", () => {
  it("live presence, overview counts", () => {
    const r = mergeRoster([pres({ user_id: "a", status: "break" })], [{ ...pres({ user_id: "a", status: "available" }), resolved_24h: 4 }, { ...pres({ user_id: "b" }), claimed: 1 }]);
    expect(r).toMatchObject([{ user_id: "a", status: "break", resolved_24h: 4 }, { user_id: "b", claimed: 1 }]);
    expect(mergeRoster([pres({ user_id: "a" })], null)).toHaveLength(1);
  });
});

describe("openIncidents", () => {
  const list = [inc({ id: 1, priority: "low" }), inc({ id: 2, priority: "high", org_id: "o2", org_name: "Beta" }), inc({ id: 3, escalation_level: 2 }),
    inc({ id: 4, state: "pending_verify" }), inc({ id: 5, state: "closed" })];
  it("orders escalated first, then priority", () => {
    expect(openIncidents(list, NO_OPEN_FILTERS).map((i) => i.id)).toEqual([3, 2, 4, 1]);
  });
  it("filters by customer, state and escalation floor", () => {
    expect(openIncidents(list, { ...NO_OPEN_FILTERS, org: "o2" }).map((i) => i.id)).toEqual([2]);
    expect(openIncidents(list, { ...NO_OPEN_FILTERS, state: "pending_verify" }).map((i) => i.id)).toEqual([4]);
    expect(openIncidents(list, { ...NO_OPEN_FILTERS, escalation: 1 }).map((i) => i.id)).toEqual([3]);
    expect(customersOf(list)).toEqual([{ id: "o1", name: "Acme" }, { id: "o2", name: "Beta" }]);
  });
});

describe("resolvedSince", () => {
  it("lists closed and awaiting verification since the shift began, with four-eyes", () => {
    const rows = resolvedSince([
      inc({ id: 1, state: "closed", opened_at: 1000, first_claimed_at: 1030, resolved_at: 1300, resolved_by: "b" }),
      inc({ id: 2, state: "pending_verify", opened_at: 1000, first_claimed_at: 1010, resolved_at: 1400, resolved_by: "me" }),
      inc({ id: 3, state: "pending_verify", resolved_at: 1350, resolved_by: "b" }),
      inc({ id: 4, state: "closed", resolved_at: 500 }),
    ], 1000, "me");
    expect(rows.map((r) => r.incident.id)).toEqual([2, 3, 1]);
    expect(rows[2].claimS).toBe(30);
    expect(rows[2].resolveS).toBe(300);
    expect(rows[0].canVerify).toBe(false);
    expect(rows[0].verifyBlocked).toMatch(/different supervisor/);
    expect(rows[1].canVerify).toBe(true);
  });
});

describe("withLive", () => {
  it("adds and refreshes pending rows, drops ones sent back", () => {
    const rest = [inc({ id: 1, state: "pending_verify", updated_at: 10 }), inc({ id: 2, state: "closed", updated_at: 10 }), inc({ id: 3, state: "pending_verify", updated_at: 10 })];
    const live = [inc({ id: 1, state: "claimed", updated_at: 20 }), inc({ id: 3, state: "pending_verify", updated_at: 30, disposition: "x" }), inc({ id: 4, state: "pending_verify", updated_at: 5 }), inc({ id: 5, state: "new" })];
    const r = withLive(rest, live);
    expect(r.map((i) => i.id).sort()).toEqual([2, 3, 4]);
    expect(r.find((i) => i.id === 3)?.disposition).toBe("x");
  });
});

describe("chimeDue", () => {
  it("once per open incident at level 2 or above", () => {
    const l = [inc({ id: 1, escalation_level: 2 }), inc({ id: 2, escalation_level: 1 }), inc({ id: 3, escalation_level: 3, state: "closed" }), inc({ id: 4, escalation_level: 3, state: "claimed" })];
    expect(chimeDue(l, new Set())).toEqual([1, 4]);
    expect(chimeDue(l, new Set([1]))).toEqual([4]);
  });
});

describe("SLA editor", () => {
  it("sends only what changed", () => {
    const d = slaDraft(POLICY);
    expect(d.low).toEqual({ claim: "", resolve: "", lane: "quiet" });
    expect(slaPatch(d, POLICY, parseSeconds)).toEqual({ patch: {} });
    d.high.claim = "90"; d.low.lane = "ring"; d.medium.resolve = "";
    expect(slaPatch(d, POLICY, parseSeconds)).toEqual({ patch: { high: { claim_s: 90 }, medium: { resolve_s: null }, low: { lane: "ring" } } });
  });
  it("refuses a bad number", () => {
    const d = slaDraft(POLICY);
    d.medium.claim = "1.5";
    expect(slaPatch(d, POLICY, parseSeconds)).toEqual({ error: expect.stringMatching(/^medium: time to claim/) });
  });
});
