/**
 * A server (one NVR box: the hub's `sites` row) as cards and admin actions. Used by the Site page's Servers tab,
 * the server panel and Customer → Servers, so the actions behave the same everywhere.
 */
import { useState } from "react";
import { confirmDialog, promptDialog, toast } from "@site/ui";
import { type Backup, type Server, type Site, ago, api, fmtTime } from "./api";
import { consoleHref } from "./nav";

/** Status card. With `children` (actions) it is a plain box whose name links to `href`; without, the whole card is the link. */
export function ServerCard({ s, now, href = consoleHref(s.id), children }: { s: Server; now: number; href?: string; children?: React.ReactNode }) {
  const sm = s.summary ?? {};
  const cams = sm.cameras ?? [];
  const bad = cams.filter((c) => !c.stream_ready || c.problems?.length);
  const today = Object.entries(sm.today ?? {}).map(([k, v]) => `${v} ${k}`).join(" · ");
  const diskDays = sm.disk && sm.bitrate_mbps ? Math.round(sm.disk.free_gb / ((sm.bitrate_mbps * 86400) / 8 / 1000)) : null;
  const cls = `site-card ${s.online ? "" : "offline"} ${s.retired_at ? "retired" : ""}`;
  const body = (
    <>
      <div className="head">
        <span className={`dot ${s.online ? "ok" : "bad"}`} title={s.online ? "Online" : `Offline · last seen ${ago(s.last_seen_at, now)}`} />
        {children ? <a href={href}><strong>{s.name}</strong></a> : <strong>{s.name}</strong>}
        {s.retired_at ? <span className="alert-kind">retired</span> : null}
        <span className="spacer" />
        <span className="muted small">{s.online ? "online" : `offline · ${ago(s.last_seen_at, now)}`}</span>
      </div>
      {s.location && <div className="loc">{s.location}</div>}
      <div className="stats">
        <div><span>Cameras</span> {cams.length - bad.length}/{cams.length} up</div>
        <div><span>Today</span> {today || "—"}</div>
        <div><span>Disk</span> {sm.disk ? `${sm.disk.free_gb.toLocaleString()} GB free${diskDays != null && isFinite(diskDays) ? ` · ~${diskDays} d` : ""}` : "—"}</div>
        <div><span>AI</span> {sm.yolo_ready ? "YOLO ✓" : "YOLO …"} · {sm.vlm_ready ? "Qwen ✓" : "Qwen …"}{sm.queues?.synopsis ? ` (${sm.queues.synopsis} waiting)` : ""}</div>
        <div><span>Stream</span> {sm.bitrate_mbps != null ? `${sm.bitrate_mbps} Mbps` : "—"}</div>
        <div><span>Version</span> {s.version ?? "—"}{s.clock_skew_s != null && Math.abs(s.clock_skew_s) > 30 ? ` · clock ${s.clock_skew_s > 0 ? "+" : ""}${Math.round(s.clock_skew_s)} s` : ""}</div>
      </div>
      {cams.length > 0 && (
        <div className="cams">{cams.map((c) => <span key={c.id} className={`cam ${!c.stream_ready || c.problems?.length ? "bad" : ""}`} title={(c.problems ?? []).join("; ") || (c.stream_ready ? "streaming" : "no stream")}>
          {c.name}{c.ptz && !c.ptz.at_home ? " ↗" : ""}</span>)}</div>
      )}
      {s.open_alerts > 0 && <div className="alerts">⚠ {s.open_alerts} open alert{s.open_alerts > 1 ? "s" : ""}</div>}
    </>
  );
  return children ? <div className={cls}>{body}<div className="row server-actions">{children}</div></div> : <a className={cls} href={href}>{body}</a>;
}

/**
 * Rename, note, token, backups, retire, remove, and Move to another Site. `sites` = the customer's Sites for the move
 * picker (omit to hide it). Viewers get only the console link.
 */
export function ServerActions({ s, admin, sites, onChanged }: { s: Server; admin: boolean; sites?: Pick<Site, "id" | "name">[]; onChanged: () => void }) {
  const act = (f: () => Promise<unknown>, done?: string) => async () => { try { await f(); if (done) toast.success(done); onChanged(); } catch (e) { toast.error(e); } };
  const others = (sites ?? []).filter((l) => l.id !== s.location_id);
  return (
    <>
      <a className="small" href={consoleHref(s.id)} title="The server's own interface, through its tunnel">Open server console ↗</a>
      {admin && <button className="ghost small" onClick={async () => { const name = await promptDialog("Rename server", { initial: s.name, label: "Name" }); if (name?.trim()) await act(() => api.updateServer(s.id, { name: name.trim() }))(); }}>Rename</button>}
      {admin && <button className="ghost small" title="A free-text note on where this box is (rack, room…)" onClick={async () => { const loc = await promptDialog("Where is this server?", { initial: s.location, label: "Note (rack, room…)" }); if (loc != null) await act(() => api.updateServer(s.id, { location: loc.trim() }))(); }}>Note</button>}
      {admin && others.length > 0 && (
        <select className="small" value="" aria-label="Move to site" onChange={async (e) => {
          const to = others.find((l) => l.id === e.target.value);
          if (to && await confirmDialog(`Move ${s.name} to ${to.name}?`, { message: "Its cameras, alerts and events follow it. People who can see only the old site stop seeing it.", confirmLabel: "Move" }))
            await act(() => api.updateServer(s.id, { location_id: to.id }), `${s.name} moved to ${to.name}`)();
        }}>
          <option value="">Move to site…</option>
          {others.map((l) => <option key={l.id} value={l.id}>{l.name}</option>)}
        </select>
      )}
      {admin && <button className="ghost small" title="Issue a new device token (the old one stops working after 10 minutes)" onClick={async () => { if (await confirmDialog(`Rotate ${s.name}'s token?`)) await act(() => api.rotateServer(s.id), "New token sent to the server")(); }}>Rotate token</button>}
      {admin && <BackupsButton server={s} />}
      {admin && <button className="ghost small" title={s.retired_at ? "Show it in Sites, Home, Find and alerts again" : "Hide it from Sites, Home, Find and alerts (the server keeps running)"}
        onClick={async () => { if (s.retired_at || await confirmDialog(`Retire ${s.name}?`, { message: "It disappears from Sites, Home, Find, Ask and alerts. The server, its tunnel and its recordings are untouched; you can restore it under Customer → Servers.", confirmLabel: "Retire" })) await act(() => api.retireServer(s.id, !s.retired_at))(); }}>{s.retired_at ? "Restore" : "Retire"}</button>}
      {admin && <button className="ghost small" onClick={async () => { if (await confirmDialog(`Remove ${s.name}?`, { message: "The server is told to unenroll; recordings stay on it.", confirmLabel: "Remove", danger: true })) await act(() => api.removeServer(s.id))(); }}>Remove</button>}
    </>
  );
}

export function BackupsButton({ server }: { server: Server }) {
  const [open, setOpen] = useState(false);
  const [rows, setRows] = useState<Backup[]>([]);
  const load = () => api.backups(server.id).then(setRows).catch((e) => toast.error(e));
  return (
    <>
      <button className="ghost small" onClick={() => { setOpen(true); load(); }}>Backups</button>
      {open && (
        <div className="modal-backdrop" onClick={() => setOpen(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()} style={{ maxWidth: 640 }}>
            <header className="modal-head"><h2>{server.name} · configuration backups</h2><button className="ghost" onClick={() => setOpen(false)} aria-label="Close">✕</button></header>
            <p className="muted small">Cameras, zones, places, rules, PTZ, neighbors, named people/vehicles, layouts, retention and briefing settings — taken nightly, 30 kept. Camera passwords are not included. Recordings and events stay on the server.</p>
            <div className="row"><button className="ghost small" onClick={async () => { try { await api.backupNow(server.id); toast.success("Backup taken"); load(); } catch (e) { toast.error(e); } }}>Back up now</button></div>
            <table className="hub-table">
              <thead><tr><th>When</th><th>Size</th><th>Cameras</th><th>Identities</th><th>Version</th><th /></tr></thead>
              <tbody>{rows.map((b) => (
                <tr key={b.id}><td>{fmtTime(b.created_at)}</td><td>{(b.bytes / 1024).toFixed(0)} KB</td><td>{b.cameras}</td><td>{b.identities}</td><td>{b.site_version}</td>
                  <td className="row">
                    <a className="small" href={`/api/sites/${server.id}/backups/${b.id}`}>Download</a>
                    <button className="ghost small" onClick={async () => {
                      if (!await confirmDialog(`Restore ${server.name} from ${fmtTime(b.created_at)}?`, { message: "Cameras, zones, rules, topology and layouts on the server are replaced; named people/vehicles are merged by name. Camera passwords must already be set on the server.", confirmLabel: "Restore", danger: true })) return;
                      try { const r = await api.restore(server.id, b.id); toast.success(`Restored: ${Object.entries(r).map(([k, v]) => `${v} ${k}`).join(", ")}`); } catch (e) { toast.error(e); }
                    }}>Restore</button>
                  </td></tr>))}
              </tbody>
            </table>
            {rows.length === 0 && <p className="muted">No backups yet.</p>}
          </div>
        </div>
      )}
    </>
  );
}
