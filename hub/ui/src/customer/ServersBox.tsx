/** Customer → Servers: every server of the customer (retired ones too), the Site it belongs to, and its admin actions. */
import type { Server, Site } from "../api";
import { ago } from "../api";
import { go, siteHref } from "../nav";
import { ServerActions } from "../servers";

export function ServersBox({ servers, sites, admin, onChanged }: { servers: Server[]; sites: Site[]; admin: boolean; onChanged: () => void }) {
  return (
    <div className="card">
      <h3>Servers <span className="muted small">the NVR boxes enrolled with this hub</span></h3>
      {servers.length === 0 ? <p className="muted">None yet.</p> : (
        <table className="hub-table">
          <thead><tr><th>Server</th><th>Site</th><th>Status</th><th>Host</th><th>Version</th><th /></tr></thead>
          <tbody>{servers.map((s) => (
            <tr key={s.id}>
              <td>{s.name} <span className="muted small">{s.id}</span>{s.location && <div className="muted small">{s.location}</div>}</td>
              <td>{s.location_id ? <a href={siteHref(s.location_id, "servers")} onClick={go(siteHref(s.location_id, "servers"))}>{s.location_name ?? s.location_id}</a> : <span className="muted">unassigned</span>}</td>
              <td>{s.online ? "online" : `offline · ${ago(s.last_seen_at)}`}{s.retired_at ? <> · <span className="alert-kind">retired</span></> : null}</td>
              <td className="muted small">{s.hostname}</td>
              <td>{s.version}</td>
              <td className="row"><ServerActions s={s} admin={admin} sites={sites} onChanged={onChanged} /></td>
            </tr>))}
          </tbody>
        </table>
      )}
    </div>
  );
}
