import { describe, expect, it } from "vitest";
import { keyLabel } from "../format";
import { breakdowns, normFalseAlarms, normOperators } from "./normalize";

describe("normOperators", () => {
  it("flattens soc_reports.py's nested percentiles", () => {
    const r = normOperators({
      operators: [{ user_id: "u1", email: "a@x", claimed: 4, resolved: 3, time_to_claim: { n: 4, p50: 30, p95: 80 }, time_to_resolve: { n: 3, p50: 300, p95: null },
        dispositions: { false_alarm: 2, nuisance: 1 }, escalations_received: 1, escalated_claimed: 2, false_alarm_share: 1 }],
      totals: { claimed: 4, resolved: 3, time_to_claim: { n: 4, p50: 30, p95: 80 }, time_to_resolve: { n: 0, p50: null, p95: null }, dispositions: {}, false_alarm_share: null },
    });
    expect(r.operators[0]).toEqual({ user_id: "u1", email: "a@x", claimed: 4, resolved: 3, p50_claim_s: 30, p95_claim_s: 80, p50_resolve_s: 300, p95_resolve_s: null,
      dispositions: { false_alarm: 2, nuisance: 1 }, escalations: 1, escalated_claimed: 2, false_alarm_share: 1 });
    expect(r.totals?.email).toBe("All operators");
    expect(r.totals?.p50_resolve_s).toBe(null);
  });
  it("accepts the flat list of the original contract", () => {
    const r = normOperators([{ user_id: "u1", email: "a@x", claimed: 1, resolved: 1, p50_claim_s: 5, p95_claim_s: 9, p50_resolve_s: 60, p95_resolve_s: 90, dispositions: {}, escalations: 2, false_alarm_share: 0 }]);
    expect(r.operators[0].p95_resolve_s).toBe(90);
    expect(r.operators[0].escalations).toBe(2);
    expect(r.totals).toBe(null);
  });
});

describe("normFalseAlarms", () => {
  it("reads the hub's names", () => {
    const r = normFalseAlarms({
      sites: [{ location_id: "l1", location_name: "HQ", org_name: "Acme", closed: 10, judged: 8, false_alarms: 6, rate: 0.75, top_disposition: "nuisance", last_incident_at: 99 }],
      cameras: [{ location_id: "l1", server_id: "s1", server_name: "NVR", camera_id: "c1", camera_name: "Gate", closed: 5, judged: 5, false_alarms: 4, rate: 0.8, top_disposition: "false_alarm", last_incident_at: 98 }],
      totals: { closed: 10, judged: 8, false_alarms: 6, rate: 0.75, top_disposition: null, last_incident_at: 99 },
    });
    expect(r.sites[0]).toEqual({ location_id: "l1", name: "HQ", org_name: "Acme", closed: 10, judged: 8, false: 6, rate: 0.75, top_disposition: "nuisance", last_ts: 99 });
    expect(r.cameras[0].false).toBe(4);
    expect(r.cameras[0].last_ts).toBe(98);
    expect(r.totals?.judged).toBe(8);
  });
  it("and the contract's", () => {
    const r = normFalseAlarms({ sites: [{ location_id: "l1", name: "HQ", org_name: null, closed: 2, false: 1, rate: 0.5, top_disposition: null, last_ts: 5 }], cameras: [] });
    expect(r.sites[0]).toMatchObject({ name: "HQ", false: 1, judged: 2, last_ts: 5 });
    expect(normFalseAlarms(null)).toEqual({ sites: [], cameras: [], totals: null });
  });
});

describe("breakdowns", () => {
  it("lists objects of counts, one level down too, skipping percentile summaries", () => {
    const b = breakdowns({ counts: { incidents: 3, by_priority: { high: 1, medium: 2 } }, dispositions: { false_alarm: 2 }, calls: { total: 1, by_outcome: { spoke: 1 } },
      time_to_claim: { n: 1, p50: 3, p95: 3 }, operators: [] }, keyLabel);
    expect(b).toEqual([
      { key: "counts.by_priority", label: "By priority", parts: [["medium", 2], ["high", 1]] },
      { key: "dispositions", label: "Dispositions", parts: [["false_alarm", 2]] },
      { key: "calls.by_outcome", label: "Calls · by outcome", parts: [["spoke", 1]] },
    ]);
  });
});
