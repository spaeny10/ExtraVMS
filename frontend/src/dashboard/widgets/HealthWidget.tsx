import { healthRows, type HealthRow } from "../health";
import type { DashboardSource, SourceSite } from "../source";
import type { Widget } from "../types";

const gb = (n: number) => (n >= 1000 ? `${(n / 1000).toFixed(1)} TB` : `${Math.round(n)} GB`);

/**
 * One line per Site (on the hub; per server where there are no Sites): online, cameras up, disk and open alerts, Sites
 * with a problem first. A several-server Site with a problem lists the servers that have one under it, so the line says
 * which box. Data comes from the source's fleet snapshot; `props.sites` (server ids) narrows it.
 */
export function HealthWidget({ widget: w, source }: { widget: Widget<"health">; source: DashboardSource }) {
  const sites = source.sites().filter((s) => !w.props.sites?.length || w.props.sites.includes(s.id));
  if (sites.length === 0) return <div className="dash-empty muted small">No sites to show.</div>;
  return (
    <div className="dash-health">
      {healthRows(sites).map((r) => (
        <div key={r.key} className="dash-site-group">
          <SiteLine r={r} href={source.liveHref(r.servers[0].id)} />
          {r.problemServers.map((s) => <ServerLine key={s.id} s={s} href={source.liveHref(s.id)} />)}
        </div>
      ))}
    </div>
  );
}

function SiteLine({ r, href }: { r: HealthRow; href: string }) {
  const one = r.servers.length === 1 ? r.servers[0] : null;
  const down = r.servers.length - r.online;
  const dot = down === 0 ? "ok" : r.online > 0 ? "warn" : "bad";
  return (
    <a className={`dash-site ${r.online ? "" : "offline"}`} href={href}>
      <span className={`dot ${dot}`} />
      <strong>{r.name}</strong>
      {r.online > 0
        ? <span className="muted small">{r.camerasUp}/{r.cameras} cams</span>
        : <span className="muted small">offline</span>}
      {!one && down > 0 && r.online > 0 && <span className="muted small">{r.online}/{r.servers.length} servers</span>}
      {one && <Details s={one} />}
      <span className="spacer" />
      <Problems alerts={r.openAlerts} camsDown={r.online > 0 ? r.servers.filter((s) => s.online).reduce((n, s) => n + s.cameras - s.camerasUp, 0) : 0} />
    </a>
  );
}

function ServerLine({ s, href }: { s: SourceSite; href: string }) {
  return (
    <a className={`dash-site dash-server ${s.online ? "" : "offline"}`} href={href}>
      <span className={`dot ${s.online ? "ok" : "bad"}`} />
      <span>{s.name}</span>
      <span className="muted small">{s.online ? `${s.camerasUp}/${s.cameras} cams` : "offline"}</span>
      <Details s={s} />
      <span className="spacer" />
      <Problems alerts={s.openAlerts} camsDown={s.online ? s.cameras - s.camerasUp : 0} />
    </a>
  );
}

function Details({ s }: { s: SourceSite }) {
  return <>
    {s.online && s.diskFreeGb != null && <span className="muted small">{gb(s.diskFreeGb)} free</span>}
    {s.online && s.bandwidth && <span className="muted small" title="Video from the cameras (5-min average) · received this month">{s.bandwidth.mbps.toFixed(1)} Mbit/s · {s.bandwidth.month_gb.toFixed(0)} GB/mo</span>}
  </>;
}

function Problems({ alerts, camsDown }: { alerts: number; camsDown: number }) {
  return <>
    {alerts > 0 && <span className="bad-text small">{alerts} alert{alerts > 1 ? "s" : ""}</span>}
    {camsDown > 0 && <span className="bad-text small">{camsDown} cam down</span>}
  </>;
}
