/**
 * SOC reports (/soc/reports/:report): segmented tabs (links, so a report can be bookmarked or opened in a new tab)
 * over the four reports in soc/reports/. Each report fetches its own data when it opens and when its period changes.
 */
import type { Me } from "../api";
import { isSocSupervisor } from "../access";
import { type SocReport, SOC_REPORTS, go, reportHref, socHref } from "../nav";
import { CustomerSummary } from "./reports/CustomerSummary";
import { FalseAlarmsReport } from "./reports/FalseAlarmsReport";
import { OperatorsReport } from "./reports/OperatorsReport";
import { ShiftReport } from "./reports/ShiftReport";

export const REPORT_LABEL: Record<SocReport, string> = { operators: "Operators", "false-alarms": "False alarms", shifts: "Shift reports", customers: "Customer summary" };

export function ReportsPage({ me, report }: { me: Me; report: SocReport }) {
  // the hub answers 403 to operators on every report route; say so once instead of four errors
  if (!isSocSupervisor(me)) {
    return (
      <div className="card soc-placeholder">
        <h2>Supervisors only</h2>
        <p className="muted">SOC reports are for supervisors. <a href={socHref()} onClick={go(socHref())}>Back to the queue</a></p>
      </div>
    );
  }
  return (
    <div className="soc-reports">
      <div className="segmented soc-report-tabs" role="tablist" aria-label="Reports">
        {SOC_REPORTS.map((r) => (
          <a key={r} role="tab" aria-selected={r === report} className={r === report ? "active" : ""} href={reportHref(r)} onClick={go(reportHref(r))}>{REPORT_LABEL[r]}</a>
        ))}
      </div>
      {report === "operators" ? <OperatorsReport me={me} />
        : report === "false-alarms" ? <FalseAlarmsReport me={me} />
        : report === "shifts" ? <ShiftReport me={me} />
        : <CustomerSummary me={me} />}
    </div>
  );
}
