/**
 * Pure model of a Site's SOC arming (Site → Settings → Monitoring): the weekly schedule, holidays and manual override
 * as the hub stores them (hub/hub/soc.py armed_now), plus the per-day shape the editor works on.
 *
 * Every time is wall-clock in the Site's timezone, so "armed 22:00 → 06:00" follows DST the way people expect. The
 * hub decides what is armed; this copy only previews it (next change, editor labels) and must agree with it on the
 * rules: an unexpired override wins, then a holiday for the date, then the weekly windows; a monitored Site with no
 * windows is armed around the clock. Reasons use the hub's codes (unmonitored, override, holiday, schedule,
 * disarmed_schedule, always). `dow` is 0 = Monday … 6 = Sunday (Python's weekday()).
 * Kept free of React so it can be unit-tested.
 */
import type { ArmHoliday, ArmOverride, ArmWindow } from "../api";

export const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"] as const;
const DAY_MIN = 1440;

/** The fields of GET /monitoring this model reads. */
export type ArmModel = { monitored: boolean; arm_schedule: ArmWindow[]; arm_holidays: ArmHoliday[]; arm_override: ArmOverride | null };
/** One window of one day in the editor. */
export type Span = { from: string; to: string };
/** The editor's shape: seven lists of windows, Monday first, each window owned by the day it starts on. */
export type Week = Span[][];

const HHMM = /^([01]\d|2[0-3]):([0-5]\d)$/;
export const isTime = (s: string | undefined | null): s is string => !!s && HHMM.test(s);
export const toMin = (s: string) => { const m = HHMM.exec(s); return m ? Number(m[1]) * 60 + Number(m[2]) : 0; };
export const fromMin = (n: number) => { const x = ((n % DAY_MIN) + DAY_MIN) % DAY_MIN; return `${String(Math.floor(x / 60)).padStart(2, "0")}:${String(x % 60).padStart(2, "0")}`; };
/** `to <= from` runs past midnight (from == to is a full 24 h starting at `from`). */
export const isOvernight = (s: Span) => toMin(s.to) <= toMin(s.from);
/** End in minutes after the start day's midnight (overnight windows end on the next day, up to 2880). */
const endOf = (s: Span) => (isOvernight(s) ? toMin(s.to) + DAY_MIN : toMin(s.to));

/** "22:00 → 06:00 (next day)"; "all day" for 00:00 → 00:00. */
export function windowLabel(s: Span): string {
  if (toMin(s.from) === 0 && toMin(s.to) === 0) return "all day";
  return `${s.from} → ${s.to}${isOvernight(s) ? " (next day)" : ""}`;
}

/** Merge one day's windows: overlapping or touching ones join; nothing runs longer than 24 h from its start. */
export function mergeSpans(spans: Span[]): Span[] {
  const iv = spans.filter((s) => isTime(s.from) && isTime(s.to)).map((s) => [toMin(s.from), endOf(s)] as [number, number]).sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const out: [number, number][] = [];
  for (const [a, b] of iv) {
    const last = out[out.length - 1];
    if (last && a <= last[1]) last[1] = Math.max(last[1], b);
    else out.push([a, b]);
  }
  return out.map(([a, b]) => ({ from: fromMin(a), to: fromMin(Math.min(b, a + DAY_MIN)) }));
}

/** Wire windows → the editor's week (a window listed for several days appears on each). */
export function toWeek(windows: ArmWindow[]): Week {
  const week: Week = DAYS.map(() => []);
  for (const w of windows) for (const d of w.dow) if (d >= 0 && d < 7) week[d].push({ from: w.from, to: w.to });
  return week.map(mergeSpans);
}

/** The editor's week → canonical wire windows: merged per day, days with the same window share one entry. */
export function fromWeek(week: Week): ArmWindow[] {
  const groups = new Map<string, ArmWindow>();
  week.forEach((spans, d) => mergeSpans(spans).forEach((s) => {
    const k = `${s.from}-${s.to}`;
    const g = groups.get(k);
    if (g) g.dow.push(d); else groups.set(k, { dow: [d], from: s.from, to: s.to });
  }));
  return [...groups.values()].sort((a, b) => a.dow[0] - b.dow[0] || toMin(a.from) - toMin(b.from));
}

/**
 * Canonical form of any window list: merge overlaps within each start day (overnight ones included), drop invalid
 * times, group days that share a window. Idempotent, so comparing normalized lists tells whether anything changed.
 */
export const normalizeWindows = (windows: ArmWindow[]): ArmWindow[] => fromWeek(toWeek(windows));

/** Copy one day's windows onto other days ("Mon → Tue–Fri", "→ weekend"). */
export function copyDay(week: Week, from: number, to: number[]): Week {
  return week.map((spans, d) => (to.includes(d) && d !== from ? week[from].map((s) => ({ ...s })) : spans));
}

// ---------------------------------------------------------------- time zones

/** Wall-clock parts in the Site's zone: date "YYYY-MM-DD", dow 0 = Monday, minute of the day. */
export type LocalParts = { date: string; dow: number; min: number };

const fmts = new Map<string, Intl.DateTimeFormat>();
function fmtFor(tz: string) {
  let f = fmts.get(tz);
  if (!f) {
    f = new Intl.DateTimeFormat("en-US", { timeZone: tz, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
    fmts.set(tz, f);
  }
  return f;
}

export function validTimeZone(tz: string | null | undefined): tz is string {
  if (!tz) return false;
  try { fmtFor(tz); return true; } catch { return false; }
}

/** The zone's UTC offset at `ts` in seconds (local wall clock = ts + offset). */
export function tzOffset(ts: number, tz: string): number {
  const t = Math.floor(ts);
  const p: Record<string, number> = {};
  for (const x of fmtFor(tz).formatToParts(new Date(t * 1000))) if (x.type !== "literal") p[x.type] = Number(x.value);
  return Date.UTC(p.year, p.month - 1, p.day, p.hour % 24, p.minute, p.second) / 1000 - t;
}

const partsWithOffset = (ts: number, off: number): LocalParts => {
  const d = new Date((Math.floor(ts) + off) * 1000);
  const date = `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}-${String(d.getUTCDate()).padStart(2, "0")}`;
  return { date, dow: (d.getUTCDay() + 6) % 7, min: d.getUTCHours() * 60 + d.getUTCMinutes() };
};

export const localParts = (ts: number, tz: string): LocalParts => partsWithOffset(ts, tzOffset(ts, tz));

// ---------------------------------------------------------------- armed state

export type ArmState = { armed: boolean; reason: string };

/** As soc.active_override: a known mode and `until` still ahead. */
const overrideActive = (o: ArmOverride | null, ts: number): o is ArmOverride => !!o && (o.mode === "arm" || o.mode === "disarm") && Number(o.until) > ts;

function holidayFor(m: ArmModel, date: string): ArmHoliday | undefined {
  return m.arm_holidays.find((h) => h.date === date);
}

function windowsCover(windows: ArmWindow[], p: LocalParts): boolean {
  const prev = (p.dow + 6) % 7;
  return windows.some((w) => {
    if (!isTime(w.from) || !isTime(w.to)) return false;
    const a = toMin(w.from), end = endOf(w);
    // today's start …, or the tail of yesterday's overnight window
    return (w.dow.includes(p.dow) && p.min >= a && p.min < Math.min(end, DAY_MIN)) || (end > DAY_MIN && w.dow.includes(prev) && p.min < end - DAY_MIN);
  });
}

/** Armed state for the given wall-clock parts (the override is checked against `ts`). */
function armedFor(m: ArmModel, ts: number, p: LocalParts): ArmState {
  if (!m.monitored) return { armed: false, reason: "unmonitored" };
  if (overrideActive(m.arm_override, ts)) return { armed: m.arm_override.mode === "arm", reason: "override" };
  const h = holidayFor(m, p.date);
  if (h) {
    if (!h.armed) return { armed: false, reason: "holiday" };
    if (isTime(h.from) && isTime(h.to)) {
      // a holiday's hours stay within its date: an overnight from/to means before `to` or from `from` that day
      const a = toMin(h.from), b = toMin(h.to);
      return { armed: b > a ? p.min >= a && p.min < b : p.min >= a || p.min < b, reason: "holiday" };
    }
    return { armed: true, reason: "holiday" };
  }
  if (!m.arm_schedule.length) return { armed: true, reason: "always" };
  return windowsCover(m.arm_schedule, p) ? { armed: true, reason: "schedule" } : { armed: false, reason: "disarmed_schedule" };
}

export function isArmedAt(m: ArmModel, ts: number, tz: string): ArmState {
  return armedFor(m, ts, localParts(ts, tz));
}

/**
 * When the armed state next flips after `ts` (epoch seconds), looking up to `horizonDays` ahead; null if it doesn't.
 * Walks minute by minute with the zone offset refreshed every UTC half hour (DST changes happen on those, St John's
 * included), so it costs a few hundred Intl calls rather than thousands.
 */
export function nextChange(m: ArmModel, ts: number, tz: string, horizonDays = 8): number | null {
  const start = Math.floor(ts);
  const now = isArmedAt(m, start, tz).armed;
  const until = overrideActive(m.arm_override, start) ? m.arm_override.until : null;
  let off = tzOffset(start, tz);
  let prev = start;
  for (let t = Math.floor(start / 60) * 60 + 60; t <= start + horizonDays * 86400; t += 60) {
    if (until != null && until > prev && until < t && armedFor(m, until, partsWithOffset(until, tzOffset(until, tz))).armed !== now) return until;
    if (t % 1800 === 0) off = tzOffset(t, tz);
    if (armedFor(m, t, partsWithOffset(t, off)).armed !== now) return t;
    prev = t;
  }
  return null;
}

/** Plain words for the hub's reason codes. */
export const REASON_LABEL: Record<string, string> = {
  unmonitored: "not monitored", override: "manual override", holiday: "holiday", schedule: "inside the armed hours",
  disarmed_schedule: "outside the armed hours", always: "no armed hours set: armed around the clock",
};

/** The arm/disarm-now durations; "next" = until the schedule would change anyway (capped at the hub's 24 h). */
export const OVERRIDE_DURATIONS = [["next", "until the next scheduled change"], ["1", "for 1 hour"], ["4", "for 4 hours"], ["24", "for 24 hours"]] as const;
export type OverrideDuration = (typeof OVERRIDE_DURATIONS)[number][0];

export function overrideUntil(choice: OverrideDuration, m: ArmModel, now: number, tz: string): number {
  const cap = Math.floor(now) + 24 * 3600;
  if (choice !== "next") return Math.min(cap, Math.floor(now) + Number(choice) * 3600);
  // the schedule's own next change, ignoring any override in force now
  return Math.min(cap, nextChange({ ...m, arm_override: null }, now, tz, 1) ?? cap);
}

/** "Tue 06:00" in the Site's zone (with the date when it is more than six days away). */
export function fmtInZone(ts: number, tz: string, now = Date.now() / 1000): string {
  const opts: Intl.DateTimeFormatOptions = { timeZone: tz, weekday: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23" };
  if (ts - now > 6 * 86400) Object.assign(opts, { month: "short", day: "numeric" });
  return new Intl.DateTimeFormat(undefined, opts).format(new Date(ts * 1000));
}
