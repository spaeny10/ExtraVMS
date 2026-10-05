/**
 * Site → Alerts: what the SOC did at this Site, above the customer's own alerts. Stage-1 placeholder: the read-only
 * incident list and log come with the incidents API (GET /api/locations/{id}/incidents). Renders nothing for a Site
 * the SOC doesn't monitor, so customers without the service never see an empty SOC box.
 */
import { useEffect, useState } from "react";
import { type Site, api } from "../api";

export function SiteIncidents({ site }: { site: Site }) {
  // the Site rollup may carry `monitored`; older hubs don't, so ask the monitoring endpoint (a 404 = no SOC here)
  const [monitored, setMonitored] = useState<boolean | null>(site.monitored ?? null);
  useEffect(() => {
    if (site.monitored !== undefined) { setMonitored(site.monitored); return; }
    let alive = true;
    api.monitoring(site.id).then((m) => { if (alive) setMonitored(m.monitored); }).catch(() => { if (alive) setMonitored(false); });
    return () => { alive = false; };
  }, [site.id, site.monitored]);
  if (!monitored) return null;
  return (
    <div className="card site-incidents">
      <h3 style={{ marginTop: 0 }}>SOC incidents</h3>
      <p className="muted" style={{ margin: 0 }}>No SOC incidents yet.</p>
      <p className="muted small" style={{ margin: "4px 0 0" }}>SOC incidents for this Site will appear here, with what the operator did about each.</p>
    </div>
  );
}
