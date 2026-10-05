import { describe, expect, it } from "vitest";
import { type ArmModel, copyDay, fromWeek, isArmedAt, localParts, mergeSpans, nextChange, normalizeWindows, overrideUntil, toWeek, tzOffset, windowLabel } from "./monitoring";

const TZ = "America/Chicago";
/** Epoch seconds of a UTC wall time. */
const utc = (s: string) => Date.parse(`${s}Z`) / 1000;
const model = (m: Partial<ArmModel> = {}): ArmModel => ({ monitored: true, arm_schedule: [], arm_holidays: [], arm_override: null, ...m });
// 2026-01-05 is a Monday; Chicago is UTC-6 in January
const MON_2100 = utc("2026-01-06T03:00:00");

describe("windows", () => {
  it("labels overnight windows", () => {
    expect(windowLabel({ from: "22:00", to: "06:00" })).toBe("22:00 → 06:00 (next day)");
    expect(windowLabel({ from: "08:00", to: "17:00" })).toBe("08:00 → 17:00");
    expect(windowLabel({ from: "00:00", to: "00:00" })).toBe("all day");
  });
  it("merges overlapping and touching windows, overnight ones included", () => {
    expect(mergeSpans([{ from: "18:00", to: "22:00" }, { from: "21:00", to: "02:00" }, { from: "08:00", to: "09:00" }, { from: "09:00", to: "10:00" }]))
      .toEqual([{ from: "08:00", to: "10:00" }, { from: "18:00", to: "02:00" }]);
    // nothing longer than 24 h from its start
    expect(mergeSpans([{ from: "06:00", to: "05:00" }, { from: "20:00", to: "08:00" }])).toEqual([{ from: "06:00", to: "06:00" }]);
    expect(mergeSpans([{ from: "25:00", to: "06:00" }])).toEqual([]);
  });
  it("normalises: per-day merge, shared windows grouped, idempotent", () => {
    const n = normalizeWindows([{ dow: [0, 1], from: "22:00", to: "06:00" }, { dow: [1], from: "23:00", to: "01:00" }, { dow: [5, 6], from: "00:00", to: "00:00" }]);
    expect(n).toEqual([{ dow: [0, 1], from: "22:00", to: "06:00" }, { dow: [5, 6], from: "00:00", to: "00:00" }]);
    expect(normalizeWindows(n)).toEqual(n);
  });
  it("week round trip and copy day", () => {
    const week = toWeek([{ dow: [0], from: "18:00", to: "06:00" }]);
    expect(week[0]).toEqual([{ from: "18:00", to: "06:00" }]);
    const copied = copyDay(week, 0, [1, 2, 3, 4]);
    expect(fromWeek(copied)).toEqual([{ dow: [0, 1, 2, 3, 4], from: "18:00", to: "06:00" }]);
    expect(copied[5]).toEqual([]);
    copied[1][0].from = "19:00";
    expect(week[0][0].from).toBe("18:00");   // copies, not shared objects
  });
});

describe("time zone parts", () => {
  it("reads Chicago wall time, Monday = 0", () => {
    expect(localParts(MON_2100, TZ)).toEqual({ date: "2026-01-05", dow: 0, min: 21 * 60 });
    expect(tzOffset(MON_2100, TZ)).toBe(-6 * 3600);
    // JS getDay() has Sunday = 0; the hub (Python weekday()) has Sunday = 6
    expect(localParts(MON_2100 - 86400, TZ).dow).toBe(6);
    expect(localParts(MON_2100 + 5 * 86400, TZ).dow).toBe(5);
    expect(tzOffset(utc("2026-07-01T12:00:00"), TZ)).toBe(-5 * 3600);
  });
});

describe("isArmedAt", () => {
  const night = model({ arm_schedule: [{ dow: [0], from: "22:00", to: "06:00" }] });
  it("an overnight window runs into the next day", () => {
    expect(isArmedAt(night, MON_2100, TZ)).toEqual({ armed: false, reason: "disarmed_schedule" });
    expect(isArmedAt(night, MON_2100 + 3600, TZ)).toEqual({ armed: true, reason: "schedule" });
    expect(isArmedAt(night, MON_2100 + 8 * 3600 + 59 * 60, TZ).armed).toBe(true);   // Tue 05:59
    expect(isArmedAt(night, MON_2100 + 9 * 3600, TZ).armed).toBe(false);            // Tue 06:00
    expect(isArmedAt(night, MON_2100 + 24 * 3600 + 3600, TZ).armed).toBe(false);     // Tue 22:00: Monday only
  });
  it("not monitored, and monitored without a schedule", () => {
    expect(isArmedAt(model({ monitored: false }), MON_2100, TZ)).toEqual({ armed: false, reason: "unmonitored" });
    expect(isArmedAt(model(), MON_2100, TZ)).toEqual({ armed: true, reason: "always" });
  });
  it("a holiday replaces the schedule for its date", () => {
    const m = model({ arm_schedule: night.arm_schedule, arm_holidays: [{ date: "2026-01-05", name: "Closed", armed: true }] });
    expect(isArmedAt(m, MON_2100, TZ)).toEqual({ armed: true, reason: "holiday" });
    const off = model({ arm_schedule: night.arm_schedule, arm_holidays: [{ date: "2026-01-05", name: "Event", armed: false }] });
    expect(isArmedAt(off, MON_2100 + 3600, TZ).armed).toBe(false);
    const hours = model({ arm_holidays: [{ date: "2026-01-05", name: "Half", armed: true, from: "12:00", to: "20:00" }] });
    expect(isArmedAt(hours, MON_2100, TZ).armed).toBe(false);
    expect(isArmedAt(hours, MON_2100 - 3 * 3600, TZ).armed).toBe(true);
    // overnight hours on a holiday: before `to` or from `from`, both on that date (as soc._holiday_armed)
    const late = model({ arm_holidays: [{ date: "2026-01-05", name: "Late", armed: true, from: "20:00", to: "02:00" }] });
    expect(isArmedAt(late, MON_2100, TZ).armed).toBe(true);
    expect(isArmedAt(late, MON_2100 - 20 * 3600, TZ).armed).toBe(true);    // Mon 01:00
    expect(isArmedAt(late, MON_2100 - 12 * 3600, TZ).armed).toBe(false);   // Mon 09:00
  });
  it("an active override wins until it expires", () => {
    const m = model({ arm_schedule: night.arm_schedule, arm_override: { mode: "disarm", until: MON_2100 + 7200, by: "u", reason: "party", at: MON_2100 } });
    expect(isArmedAt(m, MON_2100 + 3600, TZ)).toEqual({ armed: false, reason: "override" });
    expect(isArmedAt(m, MON_2100 + 7200, TZ).armed).toBe(true);
  });
  it("follows DST: 22:00 → 06:00 stays wall-clock across spring forward", () => {
    // 2026-03-08 (Sunday) Chicago springs forward at 02:00; Saturday's window ends Sunday 06:00 CDT = 11:00 UTC
    const sat = model({ arm_schedule: [{ dow: [5], from: "22:00", to: "06:00" }] });
    expect(isArmedAt(sat, utc("2026-03-08T10:59:00"), TZ).armed).toBe(true);
    expect(isArmedAt(sat, utc("2026-03-08T11:00:00"), TZ).armed).toBe(false);
    expect(nextChange(sat, utc("2026-03-08T09:00:00"), TZ)).toBe(utc("2026-03-08T11:00:00"));
  });
});

describe("nextChange", () => {
  const night = model({ arm_schedule: [{ dow: [0], from: "22:00", to: "06:00" }] });
  it("finds the next flip in the Site's zone", () => {
    expect(nextChange(night, MON_2100, TZ)).toBe(MON_2100 + 3600);
    expect(nextChange(night, MON_2100 + 3600, TZ)).toBe(MON_2100 + 9 * 3600);
  });
  it("an override's expiry is a change, even off the minute", () => {
    const m = model({ arm_schedule: night.arm_schedule, arm_override: { mode: "arm", until: MON_2100 + 90, by: null, reason: "", at: MON_2100 } });
    expect(nextChange(m, MON_2100, TZ)).toBe(MON_2100 + 90);
  });
  it("null when it never changes", () => {
    expect(nextChange(model(), MON_2100, TZ)).toBeNull();
  });
  it("override until: the schedule's next change, capped at 24 h", () => {
    expect(overrideUntil("next", night, MON_2100, TZ)).toBe(MON_2100 + 3600);
    expect(overrideUntil("4", night, MON_2100, TZ)).toBe(MON_2100 + 4 * 3600);
    expect(overrideUntil("next", model(), MON_2100, TZ)).toBe(MON_2100 + 24 * 3600);
  });
});
