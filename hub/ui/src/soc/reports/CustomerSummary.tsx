/**
 * Customer summary: one customer's month as the SOC saw it (incidents, response, calls, armed hours, per Site), with
 * the text the hub writes for the customer. The hub stores the summary the first time anyone asks for a month, so
 * reopening it later shows the same report. Sending it to the customer comes with the notification release, so
 * the button is there, disabled, to show where it will be.
 */
import { useEffect, useState } from "react";
import type { Me } from "../../api";
import { keyLabel, numberTiles, reportValue } from "../format";
import { socReportsApi } from "../socApi";
import type { CustomerSummarySite } from "../types";
import { Breakdowns, CustomerSelect, copyText, useCustomers, useReport } from "./common";
import { breakdowns } from "./normalize";

const monthValue = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
// the hub's default is last month (a finished month is the one worth sending); the picker starts there too
const lastMonth = () => { const d = new Date(); return monthValue(new Date(d.getFullYear(), d.getMonth() - 1, 1)); };

/** Per-Site columns: every numeric field any Site row has, in first-seen order (time zone and flags left out). */
function siteColumns(rows: CustomerSummarySite[]): string[] {
  const cols: string[] = [];
  for (const r of rows) for (const [k, v] of Object.entries(r)) if (typeof v === "number" && !cols.includes(k)) cols.push(k);
  return cols;
}
const countsText = (v: unknown) => (v && typeof v === "object" && !Array.isArray(v)
  ? Object.entries(v as Record<string, unknown>).filter(([, n]) => typeof n === "number").sort((a, b) => (b[1] as number) - (a[1] as number))
    .map(([k, n]) => `${k.replace(/_/g, " ")} ${n}`).join(" · ") : "") || "—";

export function CustomerSummary({ me }: { me: Me }) {
  const orgs = useCustomers(me);
  const [org, setOrg] = useState("");
  const [month, setMonth] = useState(lastMonth);
  useEffect(() => { if (!org && orgs.length) setOrg(orgs[0].id); }, [orgs, org]);
  const [y, m] = month.split("-").map(Number);
  const r = useReport(() => (org && y && m ? socReportsApi.customer(org, y, m) : Promise.resolve(null)), `${org}:${month}`);
  const d = r.data;
  const sites = d?.data?.sites ?? [];
  const cols = siteColumns(sites);
  const totals = (d?.data?.totals ?? null) as Record<string, unknown> | null;
  const tiles = numberTiles(totals ? { totals } : null);
  const lines = breakdowns(totals, keyLabel);
  const orgName = orgs.find((o) => o.id === org)?.name ?? "";

  return (
    <section className="card soc-report">
      <div className="row soc-report-head">
        <h2>Customer summary</h2>
        {orgs.length === 0 ? <span className="muted small">No customers to report on.</span> : <CustomerSelect orgs={orgs} value={org} onChange={setOrg} all={null} />}
        <label className="small">Month <input type="month" value={month} max={monthValue(new Date())} onChange={(e) => e.target.value && setMonth(e.target.value)} /></label>
        <button className="ghost small" disabled={!d?.text} onClick={() => copyText(d?.text ?? "")}>Copy text</button>
        <button className="small" disabled title="coming later">Send to customer</button>
      </div>
      {!org ? null : r.error ? <p className="muted">{r.error}</p> : r.loading && !d ? <p className="muted">Loading…</p> : !d ? null : (<>
        {tiles.length > 0 && <div className="soc-tiles">{tiles.map((t) => <div key={t.key} className="soc-tile"><span className="soc-tile-value">{t.value}</span><span className="soc-tile-name">{t.label}</span></div>)}</div>}
        {lines.length > 0 && <Breakdowns lines={lines} />}
        {sites.length > 0 && (
          <div className="soc-report-scroll">
            <table className="hub-table soc-report-table">
              <thead><tr><th>Site</th>{cols.map((c) => <th key={c}>{keyLabel(c.replace(/_s$/, ""))}</th>)}<th>Dispositions</th></tr></thead>
              <tbody>
                {sites.map((s, n) => (
                  <tr key={String(s.location_id ?? s.id ?? n)}>
                    <td>{String(s.name ?? s.location_id ?? s.id ?? "—")}{s.monitored === false ? <span className="muted small"> (not monitored)</span> : null}</td>
                    {cols.map((c) => { const v = s[c]; return <td key={c}>{typeof v === "number" ? reportValue(c, v) : "—"}</td>; })}
                    <td className="small">{countsText(s.dispositions)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <h3>{orgName} · {new Date(y, m - 1, 1).toLocaleDateString(undefined, { month: "long", year: "numeric" })}</h3>
        {d.text ? <pre className="soc-report-text">{d.text}</pre> : <p className="muted">No summary text for this month.</p>}
      </>)}
    </section>
  );
}
