/**
 * The SOC area (/soc…): the operator queue and one incident, the supervisor view, reports. These pages work across
 * customers, so App renders them without a current customer and without the Customer picker.
 * Stage 1 is the shell only: each page is a placeholder until the incidents API and the workstation land.
 */
import type { Me } from "../api";
import { isSocSupervisor } from "../access";
import { type Route, type SocReport, SOC_REPORTS, go, reportHref, socHref } from "../nav";
import "./soc.css";

const REPORT_LABEL: Record<SocReport, string> = { operators: "Operators", "false-alarms": "False alarms", shifts: "Shift reports", customers: "Customer summary" };

export function SocRouter({ me, route }: { me: Me; route: Route }) {
  const tab = route.socTab ?? "queue";
  const supervisor = isSocSupervisor(me);
  return (
    <div className="soc-page">
      <div className="segmented soc-tabs" role="tablist">
        <a role="tab" aria-selected={tab === "queue" || tab === "incident"} className={tab === "queue" || tab === "incident" ? "active" : ""} href={socHref()} onClick={go(socHref())}>Queue</a>
        {supervisor && <a role="tab" aria-selected={tab === "supervisor"} className={tab === "supervisor" ? "active" : ""} href={socHref("supervisor")} onClick={go(socHref("supervisor"))}>Supervisor</a>}
        <a role="tab" aria-selected={tab === "reports"} className={tab === "reports" ? "active" : ""} href={reportHref()} onClick={go(reportHref())}>Reports</a>
      </div>
      {tab === "supervisor" ? (supervisor ? <SupervisorPage /> : <p className="muted">The supervisor view is for SOC supervisors.</p>)
        : tab === "reports" ? <ReportsPage report={route.report ?? "operators"} />
        : <SocPage incidentId={tab === "incident" ? route.incidentId : undefined} />}
    </div>
  );
}

/** Operator workstation: queue | incident | respond (next stage). */
export function SocPage({ incidentId }: { incidentId?: string }) {
  return (
    <div className="card soc-placeholder">
      <h2>{incidentId ? `Incident ${incidentId}` : "Queue"}</h2>
      <p className="muted">SOC queue arrives in the next stage.</p>
    </div>
  );
}

export function SupervisorPage() {
  return (
    <div className="card soc-placeholder">
      <h2>Supervisor</h2>
      <p className="muted">Operators, open incidents across customers and arming overrides arrive in a later stage.</p>
    </div>
  );
}

export function ReportsPage({ report }: { report: SocReport }) {
  return (
    <>
      <div className="segmented soc-report-tabs" role="tablist">
        {SOC_REPORTS.map((r) => (
          <a key={r} role="tab" aria-selected={r === report} className={r === report ? "active" : ""} href={reportHref(r)} onClick={go(reportHref(r))}>{REPORT_LABEL[r]}</a>
        ))}
      </div>
      <div className="card soc-placeholder">
        <h2>{REPORT_LABEL[report]}</h2>
        <p className="muted">SOC reports arrive in a later stage.</p>
      </div>
    </>
  );
}
