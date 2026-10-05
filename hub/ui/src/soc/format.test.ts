import { describe, expect, it } from "vitest";
import { age, applicableProcedures, callsByContact, cameraNames, flatSteps, incidentTitle, logText, nextUncalled } from "./format";

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
