/**
 * The SOC area (/soc…): the operator workstation (queue | incident | respond), one incident on its own page (pop-out,
 * push target), the supervisor view, reports. These pages work across customers, so App renders them without a
 * current customer and without the Customer picker, inside the SocStreamProvider (one socket, one ringer).
 * Phones get PhoneSoc for the queue and incidents; the supervisor view and reports lay themselves out for phones.
 */
import { useIsPhone } from "@site/ui";
import type { Me } from "../api";
import { isSocSupervisor } from "../access";
import { type Route, go, reportHref, socHref } from "../nav";
import { IncidentPage } from "./IncidentPage";
import { PhoneSoc } from "./PhoneSoc";
import { ReportsPage } from "./ReportsPage";
import { SocPage } from "./SocPage";
import { SupervisorPage } from "./SupervisorPage";
import "./soc.css";

export function SocRouter({ me, route }: { me: Me; route: Route }) {
  const tab = route.socTab ?? "queue";
  const supervisor = isSocSupervisor(me);
  return (
    <div className="soc-page">
      <div className="segmented soc-tabs" role="tablist">
        <a role="tab" aria-selected={tab === "queue" || tab === "incident"} className={tab === "queue" || tab === "incident" ? "active" : ""} href={socHref()} onClick={go(socHref())}>Queue</a>
        {supervisor && <a role="tab" aria-selected={tab === "supervisor"} className={tab === "supervisor" ? "active" : ""} href={socHref("supervisor")} onClick={go(socHref("supervisor"))}>Supervisor</a>}
        {/* the report routes are the supervisors' (colleagues' numbers); operators keep the queue */}
        {supervisor && <a role="tab" aria-selected={tab === "reports"} className={tab === "reports" ? "active" : ""} href={reportHref()} onClick={go(reportHref())}>Reports</a>}
      </div>
      {/* both pages say "Supervisors only" themselves, so a bookmarked link explains rather than showing nothing */}
      {tab === "supervisor" ? <SupervisorPage me={me} />
        : tab === "reports" ? <ReportsPage me={me} report={route.report ?? "operators"} />
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
