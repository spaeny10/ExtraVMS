/**
 * Site → Alerts: what the SOC did at this Site, above the customer's own alerts. Each incident expands into its
 * read-only log (calls made, procedure steps, the disposition), "Open event" (the event viewer in place, as on the
 * server's own UI) for its first event, and a link into the Site's Timeline.
 * Renders nothing for a Site the SOC doesn't monitor, so customers without the service never see an empty SOC box.
 */
import { useEffect, useState } from "react";
import { type Site, api, fmtTime } from "../api";
import { type EventRef } from "../eventOpen";
import { HubEventDetail } from "../HubEventDetail";
import { siteTimelineHref } from "../timelineLink";
import { go } from "../nav";
import { STATE_LABEL, cameraNames, eventNum, incidentTitle } from "../soc/format";
import { IncidentLog } from "../soc/IncidentLog";
import { priorityClass, priorityLabel } from "../soc/sla";
import { socApi } from "../soc/socApi";
import type { Incident } from "../soc/types";
import "../soc/soc.css";

const DAYS = 30;

export function SiteIncidents({ site }: { site: Site }) {
  // the Site rollup may carry `monitored`; older hubs don't, so ask the monitoring endpoint (a 404 = no SOC here)
  const [monitored, setMonitored] = useState<boolean | null>(site.monitored ?? null);
  useEffect(() => {
    if (site.monitored !== undefined) { setMonitored(site.monitored); return; }
    let alive = true;
    api.monitoring(site.id).then((m) => { if (alive) setMonitored(m.monitored); }).catch(() => { if (alive) setMonitored(false); });
    return () => { alive = false; };
  }, [site.id, site.monitored]);
  const [rows, setRows] = useState<Incident[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!monitored) return;
    let alive = true;
    const load = () => socApi.locationIncidents(site.id, { since: Math.floor(Date.now() / 1000) - DAYS * 86400, limit: 50 })
      .then((r) => { if (alive) { setRows(r); setError(null); } })
      .catch((e: Error) => { if (alive) setError(e.message.startsWith("404") ? null : e.message); });
    load();
    const t = setInterval(load, 60000);
    return () => { alive = false; clearInterval(t); };
  }, [monitored, site.id]);
  if (!monitored) return null;
  return (
    <div className="card site-incidents">
      <h3 style={{ marginTop: 0 }}>SOC incidents <span className="muted small">last {DAYS} days</span></h3>
      {error && <p className="muted small">Couldn't load the SOC incidents: {error}</p>}
      {!error && rows === null && <p className="muted small">Loading…</p>}
      {rows && rows.length === 0 && <p className="muted" style={{ margin: 0 }}>No SOC incidents in the last {DAYS} days.</p>}
      {rows && rows.length > 0 && (
        <ul className="site-incident-list">
          {rows.map((i) => <SiteIncidentRow key={i.id} site={site} i={i} />)}
        </ul>
      )}
    </div>
  );
}

function SiteIncidentRow({ site, i }: { site: Site; i: Incident }) {
  const cams = cameraNames(i);
  const [open, setOpen] = useState<EventRef | null>(null);
  // the list rows name the incident's cameras, not its events: the Timeline opens on the first camera at the moment
  // the incident opened (an event link when a payload does carry events)
  const first = [...(i.events ?? [])].sort((a, b) => a.ts - b.ts)[0];
  const cam = i.cameras?.[0];
  const href = first ? siteTimelineHref(site.id, first.server_id, first.camera_id, eventNum(first))
    : cam ? siteTimelineHref(site.id, cam.server_id, cam.camera_id, null, i.opened_at) : null;
  return (
    <li>
      <details>
        <summary>
          <span className={priorityClass(i.priority)} aria-label={`${priorityLabel(i.priority)} priority`}>{priorityLabel(i.priority)}</span>
          {" "}<strong>{incidentTitle(i)}</strong>
          <span className="muted small"> · {fmtTime(i.opened_at)}{cams.length ? ` · ${cams.join(", ")}` : ""} · {i.state === "closed" ? (i.disposition ? i.disposition.replace(/_/g, " ") : "closed") : STATE_LABEL[i.state] ?? i.state}</span>
        </summary>
        <div className="site-incident-body">
          {(first || href) && (
            <div className="row">
              {first && <button className="ghost small" onClick={() => setOpen({ server: first.server_id, id: eventNum(first), location: site.id })}>Open event</button>}
              {href && <a className="small" href={href} onClick={go(href)}>Open in Timeline</a>}
            </div>
          )}
          {i.disposition_notes && <p className="small">{i.disposition_notes}</p>}
          <IncidentLog rows={i.log ?? []} follow={false} />
        </div>
      </details>
      {open && first && (
        <HubEventDetail ev={open} onClose={() => setOpen(null)}
          cameraName={(cam) => i.events?.find((x) => x.server_id === open.server && x.camera_id === cam)?.camera_name
            ?? i.cameras?.find((x) => x.server_id === open.server && x.camera_id === cam)?.name ?? cam} />
      )}
    </li>
  );
}
