import type { DashboardSource } from "../source";
import type { Widget } from "../types";

/** One line per site: online, cameras up, disk, open alerts. Data comes from the source's fleet snapshot. */
export function HealthWidget({ widget: w, source }: { widget: Widget<"health">; source: DashboardSource }) {
  const sites = source.sites().filter((s) => !w.props.sites?.length || w.props.sites.includes(s.id));
  if (sites.length === 0) return <div className="dash-empty muted small">No sites to show.</div>;
  return (
    <div className="dash-health">
      {sites.map((s) => (
        <a key={s.id} className={`dash-site ${s.online ? "" : "offline"}`} href={source.liveHref(s.id)}>
          <span className={`dot ${s.online ? "ok" : "bad"}`} />
          <strong>{s.name}</strong>
          <span className="muted small">{s.online ? `${s.camerasUp}/${s.cameras} cams` : "offline"}</span>
          {s.online && s.diskFreeGb != null && <span className="muted small">{s.diskFreeGb >= 1000 ? `${(s.diskFreeGb / 1000).toFixed(1)} TB` : `${Math.round(s.diskFreeGb)} GB`} free</span>}
          <span className="spacer" />
          {s.openAlerts > 0 && <span className="bad-text small">{s.openAlerts} alert{s.openAlerts > 1 ? "s" : ""}</span>}
          {s.online && s.camerasUp < s.cameras && <span className="bad-text small">{s.cameras - s.camerasUp} cam down</span>}
        </a>
      ))}
    </div>
  );
}
