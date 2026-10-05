import { describe, expect, it } from "vitest";
import { age, applicableProcedures, callsByContact, cameraNames, flatSteps, fmtDur, incidentTitle, keyLabel, logText, nextUncalled, numberTiles, parseSeconds, percent,
  rateOf, reportRange, shiftStart, toCsv } from "./format";

describe("report helpers", () => {
  it("fmtDur", () => {
    expect(fmtDur(null)).toBe("—");
    expect(fmtDur(45.4)).toBe("45 s");
    expect(fmtDur(89)).toBe("89 s");
    expect(fmtDur(200)).toBe("3 min");
    expect(fmtDur(4800)).toBe("1 h 20 min");
    expect(fmtDur(7200)).toBe("2 h");
    expect(fmtDur(7199)).toBe("2 h");
    expect(fmtDur(86400 * 2 + 3 * 3600)).toBe("2 d 3 h");
  });
  it("percent and rateOf", () => {
    expect(percent(null)).toBe("—");
    expect(percent(0)).toBe("0%");
    expect(percent(0.004)).toBe("0.4%");
    expect(percent(0.125)).toBe("13%");
    expect(percent(1)).toBe("100%");
    expect(rateOf(1, 0)).toBe(null);
    expect(rateOf(1, 4)).toBe(0.25);
  });
  it("toCsv quotes and defuses formulas", () => {
    expect(toCsv([["a", "b,c", 'say "hi"'], [1, null, "=SUM(A1)"], ["-x", -3, "two\nlines"]]))
      .toBe("a,\"b,c\",\"say \"\"hi\"\"\"\r\n1,,'=SUM(A1)\r\n'-x,-3,\"two\nlines\"\r\n");
  });
  it("shiftStart: handovers at 06:00, 14:00 and 22:00 local", () => {
    const at = (d: number, h: number, m = 0) => new Date(2026, 9, d, h, m).getTime() / 1000;
    expect(shiftStart(at(4, 23))).toBe(at(4, 22));
    expect(shiftStart(at(4, 14))).toBe(at(4, 14));
    expect(shiftStart(at(4, 9, 30))).toBe(at(4, 6));
    expect(shiftStart(at(4, 2))).toBe(at(3, 22));
    expect(shiftStart(at(4, 20), [6, 18])).toBe(at(4, 18));
  });
  it("reportRange", () => {
    expect(reportRange("7d", 1_000_000)).toEqual({ since: 1_000_000 - 7 * 86400, until: 1_000_000 });
    expect(reportRange("custom", 5000, { since: 4000, until: 3000 })).toEqual({ since: 3000, until: 4000 });
    expect(reportRange("custom", 5000, null)).toEqual({ since: 5000 - 86400, until: 5000 });
  });
  it("numberTiles reads top-level numbers and the nested summaries", () => {
    expect(keyLabel("false_alarms")).toBe("False alarms");
    // a customer month: totals
    expect(numberTiles({ year: 2026, month: 9, start: 1, sites: [], totals: { incidents: 12, median_response_s: 42, calls: 3, armed_hours: 412.46, coverage: 0.5 } })).toEqual([
      { key: "totals.incidents", label: "Incidents", value: "12" },
      { key: "totals.median_response_s", label: "Median response", value: "42 s" },
      { key: "totals.calls", label: "Calls", value: "3" },
      { key: "totals.armed_hours", label: "Armed hours", value: (412.5).toLocaleString() },
      { key: "totals.coverage", label: "Coverage", value: "50%" },
    ]);
    // a shift: counts and calls.total, times left out
    expect(numberTiles({ start: 1, end: 2, notable_total: 1, counts: { incidents: 4, overdue: 1, by_priority: { high: 1 } }, calls: { total: 2, by_outcome: { spoke: 2 } } })).toEqual([
      { key: "notable_total", label: "Notable total", value: "1" },
      { key: "counts.incidents", label: "Incidents", value: "4" },
      { key: "counts.overdue", label: "Overdue", value: "1" },
      { key: "calls.total", label: "Calls", value: "2" },
    ]);
    expect(numberTiles(null)).toEqual([]);
  });
  it("parseSeconds", () => {
    expect(parseSeconds("")).toBe(null);
    expect(parseSeconds(" 60 ")).toBe(60);
    expect(parseSeconds("0")).toBe("invalid");
    expect(parseSeconds("abc")).toBe("invalid");
    expect(parseSeconds("90000")).toBe("invalid");
  });
});

describe("logText", () => {
  it("reads the common actions", () => {
    expect(logText({ action: "claimed", detail: null })).toBe("Claimed");
    expect(logText({ action: "note", detail: { text: "Gate open" } })).toBe("Note: Gate open");
    expect(logText({ action: "call", detail: { contact_id: 3, outcome: "no_answer" } }, [{ id: 3, name: "Jane" }])).toBe("Called Jane: No answer");
    expect(logText({ action: "sop", detail: { step_text: "Check gate", done: true } })).toBe("Ticked Check gate");
    // the hub's own detail keys
    expect(logText({ action: "call", detail: { contact_id: 3, name: "Bob", outcome: "spoke", notes: "on his way" } })).toBe("Called Bob: Spoke · on his way");
    expect(logText({ action: "sop", detail: { procedure_id: 1, title: "Intruder", step_id: "s1", text: "Check gate", done: false } })).toBe("Unticked Check gate (Intruder)");
    expect(logText({ action: "resolve", detail: { disposition: "customer_notified", notes: "told Jane", four_eyes: true } })).toBe("Resolved: Customer notified · told Jane (awaiting supervisor verification)");
    expect(logText({ action: "priority_raised", detail: { from: "low", to: "high" } })).toBe("Priority raised to high (was low)");
    expect(logText({ action: "claim", detail: { after_s: 42.3 } })).toBe("Claimed after 42 s");
    expect(logText({ action: "resolved", detail: { disposition: "false_alarm", notes: "cat" } }, [], (c) => (c === "false_alarm" ? "False alarm" : c))).toBe("Resolved: False alarm · cat");
    expect(logText({ action: "relay", detail: { on: true, camera_name: "Gate" } })).toBe("Relay switched on (Gate)");
  });
  it("reads a supervisor's rejection", () => {
    expect(logText({ action: "rejected", detail: { note: "call the keyholder", to: "claimed" } })).toBe("Sent back by a supervisor: call the keyholder");
  });
  it("falls back to the bare action", () => {
    expect(logText({ action: "something_new", detail: "x" })).toBe("Something new: x");
  });
});

it("age, cameras, title", () => {
  expect(age(100, 145)).toBe("45 s");
  expect(age(100, 400)).toBe("5 min");
  expect(cameraNames({ events: [{ camera_name: "A" }, { camera_name: "B" }, { camera_name: "A" }] as never })).toEqual(["A", "B"]);
  expect(incidentTitle({ id: 4, title: null, events: [{ kind: "person_loitering" }] as never })).toBe("Person loitering");
  expect(incidentTitle({ id: 4, title: null, events: [] })).toBe("Incident #4");
});

describe("respond helpers", () => {
  const procs = [
    { id: 2, order: 1, title: "High only", category: null, priority: "high" as const, steps: [{ id: "a", text: "Call police", required: true }] },
    { id: 1, order: 0, title: "Always", category: null, priority: null, steps: [{ id: "x", text: "Look", required: true }, { text: "Note", required: false }] },
  ];
  it("reads step state the hub puts on each step", () => {
    const withState = [{ id: 1, order: 0, title: "T", category: null, priority: null, steps: [{ id: "x", text: "Look", required: true, done: true, by: "a@b" }] }];
    expect(flatSteps(withState, "low")[0]).toMatchObject({ done: true, by: "a@b", n: 1 });
  });
  it("applicable procedures and numbered steps with progress", () => {
    expect(applicableProcedures(procs, "medium").map((p) => p.id)).toEqual([1]);
    const steps = flatSteps(procs, "high", { "1": { x: { done: true, by: "a@b", at: 1 } } });
    expect(steps.map((s) => [s.n, s.stepId, s.done])).toEqual([[1, "x", true], [2, "s2", false], [3, "a", false]]);
  });
  it("next uncalled contact", () => {
    const contacts = [{ id: 7, order: 1 }, { id: 5, order: 0 }];
    const log = [{ id: 1, ts: 1, user_id: null, user_email: null, action: "call", detail: { contact_id: 5, outcome: "busy" } }];
    expect(nextUncalled(contacts, log)?.id).toBe(7);
    expect(callsByContact(log).get(5)?.length).toBe(1);
    expect(nextUncalled(contacts, [...log, { ...log[0], id: 2, detail: { contact_id: 7, outcome: "spoke" } }])).toBe(null);
  });
});
