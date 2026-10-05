/**
 * Operators: per SOC operator over a period, how much they claimed and resolved, how fast (median and 95th
 * percentile, computed by the hub), what they decided, and how many of their incidents escalated. The CSV export is
 * the table on screen, built in the browser, with raw seconds so a spreadsheet can do its own maths.
 */
import { useState } from "react";
import type { Me } from "../../api";
import { fmtDur, percent, toCsv } from "../format";
import { socReportsApi } from "../socApi";
import { useSoc } from "../useSocStream";
import type { OperatorStat } from "../types";
import { CustomerSelect, DEFAULT_RANGE, RangePicker, type RangeValue, fileDate, rangeOf, saveText, useCustomers, useReport } from "./common";

const topDispositions = (d: Record<string, number>, n = 3) => Object.entries(d ?? {}).sort((a, b) => b[1] - a[1]).slice(0, n);

function Row({ o, label, total = false }: { o: OperatorStat; label: (c: string) => string; total?: boolean }) {
  return (
    <tr className={total ? "soc-report-total" : ""}>
      <td>{o.email}</td><td>{o.claimed}</td><td>{o.resolved}</td>
      <td>{fmtDur(o.p50_claim_s)}</td><td>{fmtDur(o.p95_claim_s)}</td><td>{fmtDur(o.p50_resolve_s)}</td><td>{fmtDur(o.p95_resolve_s)}</td>
      <td title={o.escalated_claimed ? `Also picked up ${o.escalated_claimed} already escalated` : undefined}>{total ? "" : o.escalations}</td><td>{percent(o.false_alarm_share)}</td>
      <td className="small">{topDispositions(o.dispositions).map(([k, v]) => `${label(k)} ${v}`).join(" · ") || "—"}</td>
    </tr>
  );
}

export function OperatorsReport({ me }: { me: Me }) {
  const { groups } = useSoc();
  const label = (code: string) => groups.flatMap((g) => g.dispositions).find((d) => d.code === code)?.label ?? code.replace(/_/g, " ");
  const [range, setRange] = useState<RangeValue>(DEFAULT_RANGE);
  const [org, setOrg] = useState("");
  const orgs = useCustomers(me);
  const r = useReport(() => { const { since, until } = rangeOf(range); return socReportsApi.operators({ since, until, org: org || undefined }); }, `${JSON.stringify(range)}:${org}`);
  const totals = r.data?.totals ?? null;
  const rows = [...(r.data?.operators ?? [])].sort((a, b) => b.resolved - a.resolved || a.email.localeCompare(b.email));

  const exportCsv = (list: OperatorStat[]) => {
    const { since, until } = rangeOf(range);
    const head = ["operator", "claimed", "resolved", "p50_claim_s", "p95_claim_s", "p50_resolve_s", "p95_resolve_s", "escalations_received", "escalated_claimed", "false_alarm_share", "dispositions"];
    const body = [...list, ...(totals ? [totals] : [])].map((o) => [o.email, o.claimed, o.resolved, o.p50_claim_s, o.p95_claim_s, o.p50_resolve_s, o.p95_resolve_s, o.escalations, o.escalated_claimed, o.false_alarm_share,
      Object.entries(o.dispositions ?? {}).map(([k, v]) => `${k}:${v}`).join("; ")]);
    saveText(`soc-operators_${fileDate(since)}_${fileDate(until)}.csv`, toCsv([head, ...body]));
  };

  return (
    <section className="card soc-report">
      <div className="row soc-report-head">
        <h2>Operators</h2>
        <RangePicker value={range} onChange={setRange} />
        <CustomerSelect orgs={orgs} value={org} onChange={setOrg} />
        <button className="ghost small" disabled={!rows.length} onClick={() => exportCsv(rows)}>Export CSV</button>
      </div>
      {r.error ? <p className="muted">{r.error}</p> : r.loading && !r.data ? <p className="muted">Loading…</p> : rows.length === 0 ? <p className="muted">No SOC work in this period.</p> : (
        <div className="soc-report-scroll">
          <table className="hub-table soc-report-table">
            <thead>
              <tr><th rowSpan={2}>Operator</th><th rowSpan={2}>Claimed</th><th rowSpan={2}>Resolved</th><th colSpan={2}>Time to claim</th><th colSpan={2}>Time to resolve</th>
                <th rowSpan={2} title="Times an incident escalated or went overdue while they held it">Escalated</th><th rowSpan={2} title="Share of their resolutions that were false alarms">False alarms</th><th rowSpan={2}>Top dispositions</th></tr>
              <tr><th>p50</th><th>p95</th><th>p50</th><th>p95</th></tr>
            </thead>
            <tbody>
              {rows.map((o) => <Row key={o.user_id} o={o} label={label} />)}
            </tbody>
            {totals && <tfoot><Row o={totals} label={label} total /></tfoot>}
          </table>
        </div>
      )}
    </section>
  );
}
