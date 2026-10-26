import { describe, expect, test } from "vitest";
import type { AiEffective, QwenDays } from "../api";
import { dayBars, dayShare, effectiveLine, fmtSeconds, kindLabel, modeLine, monthOf, outsideLine, qwenText, qwenTitle, untilText, usageShares } from "./aiSchedule";

const CHI = "America/Chicago";
// Monday 2026-10-05 12:00 CDT = 17:00 UTC
const NOON = Date.UTC(2026, 9, 5, 17, 0) / 1000;
const eff = (over: Partial<AiEffective>): AiEffective => ({ mode: "soc", outside: "catchup", qwen: "on", reason: "schedule", until: null, ...over });

describe("the effective state, in the Site's time zone", () => {
  test("on / limited until the next change", () => {
    expect(effectiveLine(eff({ qwen: "limited", reason: "disarmed_schedule", until: NOON + 6 * 3600 }), CHI, NOON)).toBe("Otso: limited until 18:00");
    expect(effectiveLine(eff({ until: NOON + 18 * 3600 }), CHI, NOON)).toBe("Otso: on until 06:00");
  });
  test("further away: the weekday, then the date", () => {
    expect(untilText(NOON + 30 * 3600, CHI, NOON)).toBe("Tue 18:00");
    expect(untilText(NOON + 8 * 86400, CHI, NOON)).toBe("Oct 13, 12:00");
    expect(untilText(NOON + 3600, "Not/AZone", NOON)).toMatch(/^\d\d:\d\d$/);   // an unknown zone falls back to the browser's
  });
  test("full time, never armed, armed around the clock", () => {
    expect(effectiveLine(eff({ mode: "full", reason: "full_time" }), CHI, NOON)).toBe("Otso: on (full time)");
    expect(effectiveLine(eff({ qwen: "limited", reason: "unmonitored" }), CHI, NOON)).toMatch(/^Otso: limited \(the Site isn't monitored/);
    expect(effectiveLine(eff({ reason: "always" }), CHI, NOON)).toBe("Otso: on (SOC hours around the clock)");
  });
});

test("the read-only lines customer admins see", () => {
  expect(modeLine("soc")).toBe("AI: SOC hours · Full-time AI available");
  expect(modeLine("full")).toBe("AI: Full-time");
  expect(outsideLine("skip")).toBe("Outside them: skip");
  expect(outsideLine("catchup")).toBe("Outside them: catch up when idle");
});

test("seconds read as seconds, minutes or hours", () => {
  expect(fmtSeconds(0)).toBe("0 s");
  expect(fmtSeconds(null)).toBe("0 s");
  expect(fmtSeconds(45.4)).toBe("45 s");
  expect(fmtSeconds(720)).toBe("12 min");
  expect(fmtSeconds(3.4 * 3600)).toBe("3.4 h");
  expect(fmtSeconds(30 * 3600)).toBe("30 h");
});

test("a month's kinds: only those used, with their share; labels for the known ones", () => {
  const z = { prompt_tokens: 0, completion_tokens: 0, out_seconds: 0 };
  const rows = usageShares([{ kind: "synopsis", requests: 3, seconds: 75, ...z }, { kind: "ppe", requests: 1, seconds: 25, ...z }, { kind: "ask", requests: 0, seconds: 0, ...z }]);
  expect(rows.map((r) => [r.kind, r.share])).toEqual([["synopsis", 75], ["ppe", 25]]);
  expect(kindLabel("catchup")).toBe("Caught-up descriptions");
  expect(kindLabel("newkind")).toBe("newkind");
});

test("the Hosts page's Qwen series", () => {
  const q: QwenDays = { days: [{ day: "2026-10-16", seconds: 0, requests: 0 }, { day: "2026-10-17", seconds: 1800, requests: 40 }, { day: "2026-10-18", seconds: 900, requests: 1 }], today_s: 900, total_s: 2700 };
  expect(qwenText(q)).toBe("15 min today · 45 min / 3 d");
  expect(qwenText(null)).toBe("—");
  expect(qwenTitle(q)).toBe("Qwen time per day (UTC):\n2026-10-16: 0 s · 0 requests\n2026-10-17: 30 min · 40 requests\n2026-10-18: 15 min · 1 request");
  expect(qwenTitle(undefined)).toBeUndefined();
  expect(dayBars(q)).toEqual([0, 1, 0.5]);
  expect(dayBars({ days: [{ day: "x", seconds: 0, requests: 0 }], today_s: 0, total_s: 0 })).toEqual([0]);
  expect(dayShare(8640)).toBe(10);
});

test("billing months are UTC months", () => {
  const oct = Date.UTC(2026, 9, 18, 12);
  expect(monthOf(0, oct)).toBe("2026-10");
  expect(monthOf(1, oct)).toBe("2026-09");
  expect(monthOf(1, Date.UTC(2026, 0, 3))).toBe("2025-12");
});
