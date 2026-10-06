/**
 * The report routes' answers, put in the shape the pages draw (normalize.test.ts). The hub's soc_reports.py nests
 * percentiles ({time_to_claim: {n, p50, p95}}) and names counts its own way (false_alarms, last_incident_at,
 * escalations_received); the flat names of the original contract are accepted too, so either hub version renders.
 */
import type { FalseAlarmCamera, FalseAlarmSite, FalseAlarms, OperatorStat } from "../types";

type Raw = Record<string, unknown>;
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const obj = (v: unknown): Raw => (v && typeof v === "object" && !Array.isArray(v) ? v as Raw : {});
const str = (v: unknown): string | null => (typeof v === "string" ? v : null);
const counts = (v: unknown): Record<string, number> => Object.fromEntries(Object.entries(obj(v)).filter(([, n]) => typeof n === "number")) as Record<string, number>;

/** One operator (or the totals) row; `email` is "All operators" for the totals. */
function operator(r: Raw, email?: string): OperatorStat {
  const ttc = obj(r.time_to_claim), ttr = obj(r.time_to_resolve);
  return {
    user_id: str(r.user_id) ?? "", email: email ?? str(r.email) ?? str(r.user_id) ?? "unknown",
    claimed: num(r.claimed) ?? 0, resolved: num(r.resolved) ?? 0,
    p50_claim_s: num(ttc.p50) ?? num(r.p50_claim_s), p95_claim_s: num(ttc.p95) ?? num(r.p95_claim_s),
    p50_resolve_s: num(ttr.p50) ?? num(r.p50_resolve_s), p95_resolve_s: num(ttr.p95) ?? num(r.p95_resolve_s),
    dispositions: counts(r.dispositions),
    escalations: num(r.escalations_received) ?? num(r.escalations) ?? 0,
    escalated_claimed: num(r.escalated_claimed) ?? undefined,
    false_alarm_share: num(r.false_alarm_share),
  };
}

/** {operators, totals} (the hub) or a bare list (the original contract). */
export function normOperators(r: unknown): { operators: OperatorStat[]; totals: OperatorStat | null } {
  if (Array.isArray(r)) return { operators: r.map((x) => operator(obj(x))), totals: null };
  const o = obj(r);
  const list = Array.isArray(o.operators) ? o.operators : [];
  return { operators: list.map((x) => operator(obj(x))), totals: o.totals ? operator(obj(o.totals), "All operators") : null };
}

function rateRow(r: Raw) {
  const closed = num(r.closed) ?? 0;
  const judged = num(r.judged);
  return {
    closed, judged: judged ?? closed, false: num(r.false_alarms) ?? num(r.false) ?? 0, rate: num(r.rate),
    top_disposition: str(r.top_disposition), last_ts: num(r.last_incident_at) ?? num(r.last_ts),
  };
}

export function normFalseAlarms(r: unknown): FalseAlarms & { totals: ReturnType<typeof rateRow> | null } {
  const o = obj(r);
  const sites: FalseAlarmSite[] = (Array.isArray(o.sites) ? o.sites : []).map((x) => {
    const s = obj(x);
    return { location_id: str(s.location_id) ?? "", name: str(s.location_name) ?? str(s.name) ?? "Site", org_name: str(s.org_name), ...rateRow(s) };
  });
  const cameras: FalseAlarmCamera[] = (Array.isArray(o.cameras) ? o.cameras : []).map((x) => {
    const c = obj(x);
    return {
      location_id: str(c.location_id) ?? "", server_id: str(c.server_id) ?? "", server_name: str(c.server_name), camera_id: str(c.camera_id) ?? "",
      camera_name: str(c.camera_name), ...rateRow(c),
    };
  });
  return { sites, cameras, totals: o.totals ? rateRow(obj(o.totals)) : null };
}

/**
 * The count breakdowns of a report's data as lines: every object of numbers at the top or one level down
 * ({by_priority: {high: 2}}, {calls: {by_outcome: {...}}}), labeled by its key ("By priority", "Calls · by outcome").
 */
export function breakdowns(data: Raw | null | undefined, labelOf: (k: string) => string): { key: string; label: string; parts: [string, number][] }[] {
  const out: { key: string; label: string; parts: [string, number][] }[] = [];
  const visit = (o: Raw, prefix: string, label: string, depth: number) => {
    for (const [k, v] of Object.entries(o)) {
      if (!v || typeof v !== "object" || Array.isArray(v)) continue;
      const entries = Object.entries(v as Raw);
      const nums = entries.filter(([, n]) => typeof n === "number") as [string, number][];
      // "counts" only groups the shift's breakdowns: its children read better without it
      const l = label && label !== labelOf("counts") ? `${label} · ${labelOf(k).toLowerCase()}` : labelOf(k);
      // {n, p50, p95} is a percentile summary, shown as durations elsewhere, not a breakdown
      if ("p50" in (v as Raw)) continue;
      if (nums.length && nums.length === entries.length) out.push({ key: prefix + k, label: l, parts: nums.sort((a, b) => b[1] - a[1]) });
      else if (depth < 1) visit(v as Raw, `${prefix}${k}.`, l, depth + 1);
    }
  };
  if (data) visit(data, "", "", 0);
  return out;
}
