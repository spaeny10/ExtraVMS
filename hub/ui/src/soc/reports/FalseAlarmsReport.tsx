/**
 * False alarms: per Site, then per camera under it, how many closed incidents were false alarms. The worst cameras
 * are where an installer should look (aim, masks, sensitivity), so each links to its lane on the Site's Timeline at
 * the last false alarm, and to the server's own Timeline as a fallback.
 */
import { useState } from "react";
import type { Me } from "../../api";
import { consoleTimelineHref, go } from "../../nav";
import { siteTimelineHref } from "../../timelineLink";
import { percent } from "../format";
import { socReportsApi } from "../socApi";
import { useSoc } from "../useSocStream";
import type { FalseAlarmCamera, FalseAlarmSite } from "../types";
import { CustomerSelect, DEFAULT_RANGE, RangePicker, type RangeValue, rangeOf, useCustomers, useReport } from "./common";

const byRate = <T extends { rate: number | null; false: number }>(a: T, b: T) => (b.rate ?? -1) - (a.rate ?? -1) || b.false - a.false;

/** A bar for a 0..1 rate; the number beside it says the same, so the bar is decoration for sighted readers only. */
const RateBar = ({ rate }: { rate: number | null }) => (
  <span className="soc-rate">
    <span className="soc-bar" aria-hidden><span style={{ width: `${Math.round(Math.min(1, Math.max(0, rate ?? 0)) * 100)}%` }} /></span>
    <span>{percent(rate)}</span>
  </span>
);

export function FalseAlarmsReport({ me }: { me: Me }) {
  const { groups } = useSoc();
  const label = (code: string | null) => (code ? groups.flatMap((g) => g.dispositions).find((d) => d.code === code)?.label ?? code.replace(/_/g, " ") : "—");
  const [range, setRange] = useState<RangeValue>({ ...DEFAULT_RANGE, choice: "7d" });
  const [org, setOrg] = useState("");
  const orgs = useCustomers(me);
  const r = useReport(() => { const { since, until } = rangeOf(range); return socReportsApi.falseAlarms({ since, until, org: org || undefined }); }, `${JSON.stringify(range)}:${org}`);
  const sites = [...(r.data?.sites ?? [])].sort(byRate);
  const totals = r.data?.totals;
  const camsOf = (s: FalseAlarmSite) => (r.data?.cameras ?? []).filter((c) => c.location_id === s.location_id).sort(byRate);
  const when = (ts: number | null) => (ts ? new Date(ts * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—");

  const camRow = (c: FalseAlarmCamera) => {
    const tl = siteTimelineHref(c.location_id, c.server_id, c.camera_id, null, c.last_ts);
    return (
      <tr key={`${c.server_id}/${c.camera_id}`} className="soc-fa-cam">
        <td><span className="soc-fa-indent">{c.camera_name || c.camera_id}</span>{c.server_name && <span className="muted small"> · {c.server_name}</span>}</td>
        <td title={`${c.closed} closed, ${c.judged} looked at`}>{c.false} / {c.judged}</td>
        <td><RateBar rate={c.rate} /></td>
        <td>{label(c.top_disposition)}</td>
        <td>{when(c.last_ts)}</td>
        <td className="soc-board-actions">
          <a className="small" href={tl} onClick={go(tl)}>Timeline</a>
          <a className="small" href={consoleTimelineHref(c.server_id, { cam: c.camera_id, t: c.last_ts ?? undefined })} target="_blank" rel="noreferrer">Open on server ↗</a>
        </td>
      </tr>
    );
  };

  return (
    <section className="card soc-report">
      <div className="row soc-report-head">
        <h2>False alarms{totals && totals.judged > 0 ? <span className="muted small"> · {totals.false} of {totals.judged} ({percent(totals.rate)})</span> : null}</h2>
        <RangePicker value={range} onChange={setRange} />
        <CustomerSelect orgs={orgs} value={org} onChange={setOrg} />
      </div>
      {r.error ? <p className="muted">{r.error}</p> : r.loading && !r.data ? <p className="muted">Loading…</p> : sites.length === 0 ? <p className="muted">No closed incidents in this period.</p> : (
        <div className="soc-report-scroll">
          <table className="hub-table soc-report-table soc-fa">
            <thead><tr><th>Site › camera</th><th title="Swept and expired incidents were never looked at, so they don't count">False / judged</th><th>Rate</th><th>Most common</th><th>Last</th><th /></tr></thead>
            <tbody>
              {sites.map((s) => [
                <tr key={s.location_id} className="soc-fa-site">
                  <td><strong>{s.name}</strong>{s.org_name && <span className="muted small"> · {s.org_name}</span>}</td>
                  <td title={`${s.closed} closed, ${s.judged} looked at`}>{s.false} / {s.judged}</td>
                  <td><RateBar rate={s.rate} /></td>
                  <td>{label(s.top_disposition)}</td>
                  <td>{when(s.last_ts)}</td>
                  <td />
                </tr>,
                ...camsOf(s).map(camRow),
              ])}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
