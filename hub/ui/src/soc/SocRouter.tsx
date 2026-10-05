/**
 * The SOC area (/soc…): the operator workstation (queue | incident | respond), one incident on its own page (pop-out,
 * push target), the supervisor view, reports. These pages work across customers, so App renders them without a
 * current customer and without the Customer picker, inside the SocStreamProvider (one socket, one ringer).
 * Phones get PhoneSoc for the queue and incidents. Supervisor and reports are placeholders until their stages land.
 */
import { useIsPhone } from "@site/ui";
import type { Me } from "../api";
import { isSocSupervisor } from "../access";
import { type Route, type SocReport, SOC_REPORTS, go, reportHref, socHref } from "../nav";
import { IncidentPage } from "./IncidentPage";
import { PhoneSoc } from "./PhoneSoc";
import { SocPage } from "./SocPage";
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
        : <Workstation me={me} incidentId={tab === "incident" ? route.incidentId : undefined} />}
    </div>
  );
}

/** The queue (/soc) or one incident (/soc/incidents/:id); a phone gets the list-and-sheet version of either. */
function Workstation({ me, incidentId }: { me: Me; incidentId?: string }) {
  const phone = useIsPhone();
  const id = incidentId ? Number(incidentId) : null;
  if (incidentId && !(id && Number.isFinite(id))) return <p className="muted">That isn't an incident number. <a href={socHref()} onClick={go(socHref())}>Queue</a></p>;
  if (phone) return <PhoneSoc me={me} incidentId={id ?? undefined} />;
  return id ? <IncidentPage me={me} id={id} /> : <SocPage me={me} />;
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
