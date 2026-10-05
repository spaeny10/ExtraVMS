/**
 * Customer → Servers: every server of the customer (retired ones too), the Site it belongs to, and its admin actions.
 * The Cards view is the old /fleet page's server cards (status, cameras, disk, AI), kept here now /fleet goes to Sites.
 */
import { useState } from "react";
import type { Server, Site } from "../api";
import { ago } from "../api";
import { go, siteHref } from "../nav";
import { ServerActions, ServerCard } from "../servers";

type View = "table" | "cards";
const VIEW_KEY = "customerServersView";
const loadView = (): View => { try { return localStorage.getItem(VIEW_KEY) === "cards" ? "cards" : "table"; } catch { return "table"; } };

export function ServersBox({ servers, sites, admin, onChanged }: { servers: Server[]; sites: Site[]; admin: boolean; onChanged: () => void }) {
  const [view, setViewState] = useState<View>(loadView);
  const setView = (v: View) => { setViewState(v); try { localStorage.setItem(VIEW_KEY, v); } catch { /* private mode */ } };
  const now = Date.now() / 1000;
  return (
    <div className="card">
      <div className="row">
        <h3 style={{ margin: 0 }}>Servers <span className="muted small">the NVR boxes enrolled with this hub</span></h3>
        <span className="spacer" />
        <div className="segmented small-seg">
          <button className={view === "table" ? "active" : ""} onClick={() => setView("table")}>Table</button>
          <button className={view === "cards" ? "active" : ""} onClick={() => setView("cards")}>Cards</button>
        </div>
      </div>
      {servers.length === 0 ? <p className="muted">None yet.</p> : view === "cards" ? (
        <div className="site-grid" style={{ marginTop: 10 }}>{servers.map((s) => <ServerCard key={s.id} s={s} now={now} />)}</div>
      ) : (
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
