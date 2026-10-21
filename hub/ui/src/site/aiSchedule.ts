/**
 * Pure helpers for a Site's AI schedule (hub/hub/ai_schedule.py) and its Qwen use: the lines the Servers tab's AI card
 * and the Hosts page show. The hub decides the state (`effective`: Qwen on or limited, until when); these only word it.
 * Kept free of React so they can be unit-tested.
 */
import type { AiEffective, AiMode, AiOutside, AiUsageKind, QwenDays } from "../api";

export const MODE_LABEL: Record<AiMode, string> = { full: "Full-time", soc: "SOC hours" };
export const OUTSIDE_LABEL: Record<AiOutside, string> = { catchup: "Catch up when idle", skip: "Skip" };
export const OUTSIDE_HELP: Record<AiOutside, string> = {
  catchup: "Events outside the SOC hours are described later, one at a time, whenever Qwen has nothing else to do.",
  skip: "Events outside the SOC hours are not described: their cards say \"No description (outside AI hours)\".",
};

/** What each job kind the servers report is called (backend aischedule.KINDS); an unknown one keeps its code. */
export const KIND_LABEL: Record<string, string> = {
  synopsis: "Event descriptions", catchup: "Caught-up descriptions", ppe: "PPE checks", plate: "Plate reads", ask: "Ask and clip chat",
  talkdown: "Describe & warn", briefing: "Briefings", journey: "Journeys", hub: "Shared AI for the hub", test: "Tests and warm-ups",
};
export const kindLabel = (k: string) => KIND_LABEL[k] ?? k;

/** "06:00" when it is within the next 24 hours, "Tue 06:00" within a week, else "Oct 21 06:00": in the Site's zone. */
export function untilText(ts: number, tz: string | null | undefined, now = Date.now() / 1000): string {
  const zone = tz || undefined;
  const opts: Intl.DateTimeFormatOptions = { timeZone: zone, hour: "2-digit", minute: "2-digit", hourCycle: "h23" };
  if (ts - now >= 6 * 86400) Object.assign(opts, { month: "short", day: "numeric" });
  else if (ts - now >= 86400) Object.assign(opts, { weekday: "short" });
  try { return new Intl.DateTimeFormat("en-US", opts).format(new Date(ts * 1000)); }
  catch { return new Intl.DateTimeFormat("en-US", { ...opts, timeZone: undefined }).format(new Date(ts * 1000)); }
}

/** "Qwen: on until 06:00" / "Qwen: limited until 18:00" / "Qwen: on (full time)" / "Qwen: limited (the Site isn't monitored)". */
export function effectiveLine(e: AiEffective, tz: string | null | undefined, now = Date.now() / 1000): string {
  if (e.mode === "full") return "Qwen: on (full time)";
  if (e.until != null) return `Qwen: ${e.qwen} until ${untilText(e.until, tz, now)}`;
  if (e.reason === "unmonitored") return "Qwen: limited (the Site isn't monitored by the SOC, so it has no SOC hours)";
  return `Qwen: ${e.qwen}${e.qwen === "on" ? " (SOC hours around the clock)" : ""}`;
}

/** The read-only line customer admins see: "AI: SOC hours · Full-time AI available" (full time: "AI: Full-time"). */
export const modeLine = (mode: AiMode): string => (mode === "full" ? "AI: Full-time" : "AI: SOC hours · Full-time AI available");

/** What happens outside the SOC hours, for a "soc" Site: "Outside them: catch up when idle". */
export const outsideLine = (outside: AiOutside): string => `Outside them: ${OUTSIDE_LABEL[outside].toLowerCase()}`;

/** "45 s", "12 min", "3.4 h" (0 = "0 s"). */
export function fmtSeconds(s: number | null | undefined): string {
  const v = Math.max(0, s ?? 0);
  if (v < 90) return `${Math.round(v)} s`;
  if (v < 5400) return `${Math.round(v / 60)} min`;
  return `${(v / 3600).toFixed(v < 36000 ? 1 : 0)} h`;
}

/** The kinds in a month's usage with at least one request, each with its share of the month's seconds (0-100). */
export function usageShares(kinds: AiUsageKind[]): (AiUsageKind & { share: number })[] {
  const total = kinds.reduce((a, k) => a + k.seconds, 0);
  return kinds.filter((k) => k.requests > 0).map((k) => ({ ...k, share: total > 0 ? Math.round((k.seconds / total) * 100) : 0 }));
}

/** A Hosts-page cell: "12 min today · 3.4 h / 7 d"; "—" without data. */
export function qwenText(q: QwenDays | null | undefined): string {
  if (!q || !q.days.length) return "—";
  return `${fmtSeconds(q.today_s)} today · ${fmtSeconds(q.total_s)} / ${q.days.length} d`;
}

/** Its tooltip: one line per UTC day, oldest first. */
export function qwenTitle(q: QwenDays | null | undefined): string | undefined {
  if (!q || !q.days.length) return undefined;
  return ["Qwen time per day (UTC):", ...q.days.map((d) => `${d.day}: ${fmtSeconds(d.seconds)} · ${d.requests} request${d.requests === 1 ? "" : "s"}`)].join("\n");
}

/** Bar heights 0..1 for a day series (the busiest day = 1); all zero without use. */
export function dayBars(q: QwenDays | null | undefined): number[] {
  const vals = (q?.days ?? []).map((d) => d.seconds);
  const top = Math.max(0, ...vals);
  return vals.map((v) => (top > 0 ? v / top : 0));
}

/** The Hosts page's share of a 24-hour day of the shared GPU a day's seconds stand for (several requests may overlap). */
export const dayShare = (seconds: number) => Math.round((seconds / 86400) * 1000) / 10;

/** "2026-10": this UTC month (back = 0) or one before it (1), as the hub's billing takes it (?month=). */
export function monthOf(back = 0, now = Date.now()): string {
  const d = new Date(now);
  const m = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() - back, 1));
  return `${m.getUTCFullYear()}-${String(m.getUTCMonth() + 1).padStart(2, "0")}`;
}
