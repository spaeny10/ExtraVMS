/**
 * Alerts across the customer, or (with `site`) one Site's servers. Event alerts open the event viewer in place (clip,
 * synopsis, feedback) as the server's own UI does; the row's href stays the Site's Timeline for middle-click, the
 * viewer's "Open in Timeline" goes there too, and ↗ is the server's console. Server alerts open the server's panel.
 * SiteAlerts is the Site page's Alerts tab: the list, a "Quiet alerts…" link to Customer › Actions and that Site's part of the digest.
 */
import { useCallback, useEffect, useState } from "react";
import { toast } from "@site/ui";
import { type Alert, type Org, type Site, ago, api, fmtTime } from "./api";
import { actionsHref } from "./customer/fleetActions";
import { type EventRef, alertEvent, cameraNameFor } from "./eventOpen";
import { HubEventDetail, useCameraNames } from "./HubEventDetail";
import { KIND_LABEL } from "./labels";
import { consoleHref, consoleTimelineHref, go, inPlace, navigate, serverHref } from "./nav";
import { DigestCard } from "./SitesPage";
import { siteTimelineHref } from "./timelineLink";

/** Where an alert row points: [in-app href or null, server console href]. */
function alertLinks(a: Alert): [string | null, string] {
  const d = a.detail as { id?: number; camera_id?: string };
  if (d.id && d.camera_id) {
    return [a.location_id ? siteTimelineHref(a.location_id, a.site_id, d.camera_id, d.id) : null, consoleTimelineHref(a.site_id, { cam: d.camera_id, event: d.id })];
  }
  return [a.location_id ? serverHref(a.location_id, a.site_id) : null, consoleHref(a.site_id, a.kind === "camera_down" ? "cameras" : "")];
}

function describe(a: Alert): string {
  const d = a.detail as Record<string, unknown>;
  return a.kind === "camera_down" ? `${d.name ?? a.key}: ${(d.problems as string[] | undefined)?.join("; ") || "no stream"}`
    : a.kind === "clock" ? `${d.skew_s} s off` : a.kind === "disk" ? String(d.message ?? "low space")
    : a.kind === "detector_fallback" || a.kind === "detector_stalled" || a.kind === "vlm_fallback_active" ? `${d.text ?? ""}${d.error ? ` (${d.error})` : ""}`
    : a.kind === "offline" ? `last seen ${ago(d.last_seen_at as number)}` : `${d.name ? `${d.name} · ` : ""}${d.text ?? d.synopsis ?? ""}`;
}

export function AlertsPage({ org, site }: { org: Org; site?: Site }) {
  const [rows, setRows] = useState<Alert[]>([]);
  const [showClosed, setShowClosed] = useState(false);
  const siteId = site?.id;
  const load = useCallback(() => (siteId ? api.locationAlerts(siteId, !showClosed) : api.alerts(org.id, !showClosed)).then(setRows).catch((e) => toast.error(e)),
    [org.id, siteId, showClosed]);
  useEffect(() => { load(); const t = setInterval(load, 15000); return () => clearInterval(t); }, [load]);
  // inside a Site the Site column is the page itself, and a one-server Site's Server column would repeat one name
  const showServer = !site || site.servers.filter((s) => !s.retired_at).length > 1;
  const [open, setOpen] = useState<EventRef | null>(null);
  const names = useCameraNames(org, site, !!open);
  return (
    <>
      {site ? <h3 style={{ marginTop: 0 }}>Alerts</h3> : <h2>Alerts <span className="muted small">{org.name}</span></h2>}
      <label className="row small"><input type="checkbox" checked={showClosed} onChange={(e) => setShowClosed(e.target.checked)} /> Include closed</label>
      {rows.length === 0 ? <p className="muted">Nothing open.</p> : (
        <table className="hub-table stack">
          <thead><tr><th>When</th>{!site && <th>Site</th>}{showServer && <th>Server</th>}<th>Kind</th><th>What</th><th /></tr></thead>
          <tbody>
            {rows.map((a) => {
              const [href, out] = alertLinks(a);
              const ev = alertEvent(a);
              const click = ev ? inPlace(() => setOpen(ev)) : href ? go(href) : undefined;
              return (
                <tr key={a.id} className={a.closed_at ? "muted" : ""}>
                  <td>{fmtTime(a.opened_at)}</td>
                  {!site && <td data-label="Site">{a.location_name ?? "—"}</td>}
                  {showServer && <td data-label="Server">{a.site_name}</td>}
                  <td><span className={`alert-kind ${a.kind}`}>{KIND_LABEL[a.kind] ?? a.kind}</span></td>
                  <td className="wide">
                    <a href={href ?? out} onClick={click} title={ev ? "Show the clip and synopsis" : undefined}>{describe(a)}</a>
                    {href && <> <a className="small muted" href={out} title="Open on server">↗</a></>}
                  </td>
                  <td className="acts">{!a.closed_at && <button className="ghost small" onClick={async () => { try { await api.ack(a.id); load(); } catch (e) { toast.error(e); } }}>Ack</button>}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      {open && <HubEventDetail ev={open} cameraName={cameraNameFor(names, open.server)} onClose={() => setOpen(null)} />}
    </>
  );
}

/**
 * The Site page's Alerts tab. "Quiet alerts…" opens Customer › Actions with the instruction prefilled (fleet actions
 * are planned and run only there: same card, role check, audit and Undo); nothing is planned until Plan is pressed.
 */
export function SiteAlerts({ org, site }: { org: Org; site: Site }) {
  const quiet = actionsHref(`Quiet alerts at ${site.name} for 2 hours`);
  return (
    <>
      <div className="row" style={{ marginBottom: 8 }}>
        <span className="spacer" style={{ flex: 1 }} />
        <button className="ghost small" onClick={() => navigate(quiet)} title="Opens Customer › Actions with this instruction filled in">Quiet alerts…</button>
      </div>
      <AlertsPage org={org} site={site} />
      <DigestCard org={org} site={site} />
    </>
  );
}
