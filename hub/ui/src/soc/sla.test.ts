import { describe, expect, it } from "vitest";
import { clock, priorityClass, priorityLabel, slaFor, slaText, slaTone } from "./sla";
import type { SlaPolicy } from "./types";

const POLICY: SlaPolicy = { high: { claim_s: 60, resolve_s: 600, ring: true }, medium: { claim_s: 180, resolve_s: 1200, ring: true }, low: { claim_s: null, resolve_s: null, ring: false } };

describe("slaTone", () => {
  it("by the fraction of the window left", () => {
    expect(slaTone(40, 60)).toBe("ok");
    expect(slaTone(30, 60)).toBe("warn");
    expect(slaTone(13, 60)).toBe("warn");
    expect(slaTone(12, 60)).toBe("urgent");
    expect(slaTone(0, 60)).toBe("breach");
    expect(slaTone(-5, 60)).toBe("breach");
  });
  it("without a window length, by seconds", () => {
    expect(slaTone(300, null)).toBe("ok");
    expect(slaTone(60, null)).toBe("warn");
    expect(slaTone(10, null)).toBe("urgent");
  });
});

describe("slaText", () => {
  it("left and over", () => {
    expect(slaText(252)).toBe("4:12 left");
    expect(slaText(251.2)).toBe("4:12 left");
    expect(slaText(-63)).toBe("+1:03 over");
    expect(slaText(0)).toBe("+0:00 over");
    expect(clock(3852)).toBe("1:04:12");
  });
});

describe("slaFor", () => {
  const base = { priority: "high" as const, opened_at: 1000, claimed_at: null, sla_due_at: 1060, resolve_due_at: null };
  it("unclaimed counts to the claim deadline", () => {
    const v = slaFor({ ...base, state: "new" }, 1010, POLICY)!;
    expect(v.kind).toBe("claim");
    expect(v.text).toBe("0:50 left");
    expect(v.tone).toBe("ok");
    expect(v.label).toBe("0:50 left to claim");
  });
  it("claimed counts to the resolve deadline", () => {
    const v = slaFor({ ...base, state: "claimed", claimed_at: 1020, resolve_due_at: 1620 }, 1600, POLICY)!;
    expect(v.kind).toBe("resolve");
    expect(v.tone).toBe("urgent");
    expect(slaFor({ ...base, state: "claimed", claimed_at: 1020, resolve_due_at: 1620 }, 1700, POLICY)!.label).toBe("Past the deadline to resolve by 1:20");
  });
  it("nothing for closed or no deadline", () => {
    expect(slaFor({ ...base, state: "closed" }, 1010, POLICY)).toBe(null);
    expect(slaFor({ ...base, state: "new", sla_due_at: null }, 1010, POLICY)).toBe(null);
  });
  it("falls back to the time since the clock started without a policy", () => {
    expect(slaFor({ ...base, state: "new" }, 1050, null)!.tone).toBe("urgent");
  });
});

it("priority badge classes reuse the threat colors", () => {
  expect(priorityClass("high")).toBe("badge threat-high");
  expect(priorityClass("weird")).toBe("badge threat-none");
  expect(priorityLabel("medium")).toBe("Medium");
});
