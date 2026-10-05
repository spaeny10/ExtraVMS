import { describe, expect, it } from "vitest";
import { EMPTY_QUEUE, NO_FILTERS, applyFilters, applyStreamMessage, isRingingUnclaimed, mergePresence, nextRinging, orderQuiet, orderRinging, queueCounts, ringPriority, splitLanes,
  stepId, upsertIncident } from "./queue";
import type { Incident } from "./types";

let seq = 0;
function inc(p: Partial<Incident> = {}): Incident {
  const id = p.id ?? ++seq;
  return {
    id, org_id: "o1", org_name: "Acme", location_id: "l1", location_name: "HQ", opened_at: 1000, last_event_at: 1000, updated_at: 1000, closed_at: null,
    state: "new", priority: "medium", lane: "ring", claimed_by: null, claimed_by_email: null, claimed_at: null, first_claimed_at: null, assigned_by: null,
    sla_due_at: 1180, resolve_due_at: null, escalation_level: 0, disposition: null, disposition_notes: null, resolved_by: null, resolved_at: null, four_eyes_by: null,
    event_count: 1, title: "Person at gate", events: [{ server_id: "s1", server_name: "NVR", event_id: 5, camera_id: "cam1", camera_name: "Gate", priority: "medium", kind: "person", ts: 1000, detail: null }],
    ...p,
  };
}

describe("lanes and order", () => {
  it("splits by lane, leaves closed out, lists pending_verify apart", () => {
    const r = splitLanes([inc({ id: 1 }), inc({ id: 2, lane: "quiet", priority: "low" }), inc({ id: 3, state: "closed" }), inc({ id: 4, state: "pending_verify" })]);
    expect(r.ring.map((i) => i.id)).toEqual([1]);
    expect(r.quiet.map((i) => i.id)).toEqual([2]);
    expect(r.verify.map((i) => i.id)).toEqual([4]);
  });
  it("rings by priority, then deadline, then age", () => {
    const list = [
      inc({ id: 1, priority: "medium", sla_due_at: 1100 }),
      inc({ id: 2, priority: "high", sla_due_at: 1300 }),
      inc({ id: 3, priority: "high", sla_due_at: 1200 }),
      inc({ id: 4, priority: "medium", sla_due_at: 1100, opened_at: 900 }),
      inc({ id: 5, priority: "medium", sla_due_at: null }),
    ];
    expect(orderRinging(list).map((i) => i.id)).toEqual([3, 2, 4, 1, 5]);
  });
  it("a claimed incident ranks by its resolve deadline", () => {
    const list = [inc({ id: 1, state: "claimed", claimed_by: "u", sla_due_at: 1000, resolve_due_at: 2000 }), inc({ id: 2, sla_due_at: 1500 })];
    expect(orderRinging(list).map((i) => i.id)).toEqual([2, 1]);
  });
  it("quiet is newest first", () => {
    expect(orderQuiet([inc({ id: 1, last_event_at: 5 }), inc({ id: 2, last_event_at: 9 })]).map((i) => i.id)).toEqual([2, 1]);
  });
});

describe("filters", () => {
  const list = [inc({ id: 1, org_id: "o1", priority: "high", claimed_by: "me" }), inc({ id: 2, org_id: "o2", org_name: "Globex", location_name: "Yard" })];
  it("customer, priority, mine, unclaimed", () => {
    expect(applyFilters(list, { ...NO_FILTERS, org: "o2" }, "me").map((i) => i.id)).toEqual([2]);
    expect(applyFilters(list, { ...NO_FILTERS, priority: "high" }, "me").map((i) => i.id)).toEqual([1]);
    expect(applyFilters(list, { ...NO_FILTERS, mine: true }, "me").map((i) => i.id)).toEqual([1]);
    expect(applyFilters(list, { ...NO_FILTERS, unclaimed: true }, "me").map((i) => i.id)).toEqual([2]);
  });
  it("text matches every word across customer, Site and cameras", () => {
    expect(applyFilters(list, { ...NO_FILTERS, text: "globex yard" }, "me").map((i) => i.id)).toEqual([2]);
    expect(applyFilters(list, { ...NO_FILTERS, text: "gate" }, "me").map((i) => i.id)).toEqual([1, 2]);
    expect(applyFilters(list, { ...NO_FILTERS, text: "acme yard" }, "me")).toEqual([]);
  });
});

describe("ringing", () => {
  it("only unclaimed ringing-lane incidents ring, the most urgent decides", () => {
    expect(isRingingUnclaimed(inc())).toBe(true);
    expect(isRingingUnclaimed(inc({ lane: "quiet" }))).toBe(false);
    expect(isRingingUnclaimed(inc({ state: "claimed", claimed_by: "u" }))).toBe(false);
    expect(ringPriority([inc({ priority: "medium" }), inc({ priority: "high", state: "claimed", claimed_by: "u" })])).toBe("medium");
    expect(ringPriority([inc({ priority: "medium" }), inc({ priority: "high" })])).toBe("high");
    expect(ringPriority([inc({ lane: "quiet", priority: "low" })])).toBe(null);
  });
  it("auto-advance picks the next unclaimed ringing incident", () => {
    const list = [inc({ id: 1, priority: "high" }), inc({ id: 2 }), inc({ id: 3, priority: "high", state: "claimed", claimed_by: "u" })];
    expect(nextRinging(list, 1)?.id).toBe(2);
    expect(nextRinging([inc({ id: 9, state: "claimed", claimed_by: "u" })], null)).toBe(null);
  });
  it("j/k steps and clamps", () => {
    expect(stepId([1, 2, 3], null, 1)).toBe(1);
    expect(stepId([1, 2, 3], 1, 1)).toBe(2);
    expect(stepId([1, 2, 3], 3, 1)).toBe(3);
    expect(stepId([1, 2, 3], 1, -1)).toBe(1);
    expect(stepId([1, 2, 3], 7, -1)).toBe(1);
    expect(stepId([], 1, 1)).toBe(null);
  });
  it("counts", () => {
    const c = queueCounts([inc({ id: 1 }), inc({ id: 2, state: "claimed", claimed_by: "me" }), inc({ id: 3, lane: "quiet" })], "me");
    expect(c).toEqual({ ringing: 2, unclaimed: 1, quiet: 1, mine: 1 });
  });
});

describe("stream reducer", () => {
  it("snapshot replaces, frames upsert, closed leaves", () => {
    let s = applyStreamMessage(EMPTY_QUEUE, { type: "snapshot", incidents: [inc({ id: 1 }), inc({ id: 2, state: "closed" })], presence: [], ring_count: 1 });
    expect(s.incidents.map((i) => i.id)).toEqual([1]);
    expect(s.ringCount).toBe(1);
    s = applyStreamMessage(s, { type: "incident_opened", incident: inc({ id: 3 }), ring_count: 2 });
    expect(s.incidents.map((i) => i.id)).toEqual([1, 3]);
    s = applyStreamMessage(s, { type: "incident_updated", incident: inc({ id: 1, state: "claimed", claimed_by: "u", updated_at: 1001 }) });
    expect(s.incidents.find((i) => i.id === 1)?.state).toBe("claimed");
    s = applyStreamMessage(s, { type: "incident_resolved", incident: inc({ id: 3, state: "closed", updated_at: 1002 }) });
    expect(s.incidents.map((i) => i.id)).toEqual([1]);
  });
  it("an older row never overwrites a newer one; slim rows keep events", () => {
    const newer = inc({ id: 1, updated_at: 2000, state: "claimed", claimed_by: "u" });
    expect(upsertIncident([newer], inc({ id: 1, updated_at: 1500 }))[0].state).toBe("claimed");
    const slim = { ...inc({ id: 1, updated_at: 2001 }), events: undefined };
    expect(upsertIncident([newer], slim)[0].events?.length).toBe(1);
  });
  it("pending_verify stays (supervisors verify it); presence and arming are kept", () => {
    let s = applyStreamMessage(EMPTY_QUEUE, { type: "incident_resolved", incident: inc({ id: 1, state: "pending_verify" }) });
    expect(s.incidents).toHaveLength(1);
    s = applyStreamMessage(s, { type: "presence", presence: [{ user_id: "u", email: "a@b", soc_role: "operator", status: "available", since: 1, incident_id: null }] });
    expect(s.presence).toHaveLength(1);
    s = applyStreamMessage(s, { type: "arming", location_id: "l1", armed: true, reason: "schedule" });
    expect(s.arming.l1).toEqual({ armed: true, reason: "schedule" });
  });
  it("presence frames carry one entry (the hub's) or a whole roster", () => {
    const a = { user_id: "a", email: "a@x", soc_role: "operator" as const, status: "available" as const, since: 1, incident_id: null };
    const b = { ...a, user_id: "b", email: "b@x" };
    expect(mergePresence([a], { ...a, status: "break" })).toEqual([{ ...a, status: "break" }]);
    expect(mergePresence([b], a).map((p) => p.user_id)).toEqual(["a", "b"]);
    expect(mergePresence([a, b], [b])).toEqual([b]);
  });
  it("text filter reads the camera names queue rows carry", () => {
    const row = inc({ id: 7, events: undefined, cameras: [{ server_id: "s", camera_id: "c", name: "Loading dock" }] });
    expect(applyFilters([row], { ...NO_FILTERS, text: "dock" }, "me")).toHaveLength(1);
  });
});
