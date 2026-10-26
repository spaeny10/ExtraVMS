/**
 * A server (one NVR box: the hub's `sites` row) as cards and admin actions. Used by the Site page's Servers tab,
 * the server panel and Customer → Servers, so the actions behave the same everywhere.
 */
import { useCallback, useEffect, useState } from "react";
import { confirmDialog, promptDialog, toast } from "@site/ui";
import { type Backup, type LiveStats, type Server, type Site, ago, api, fmtTime } from "./api";
import { consoleHref } from "./nav";
import { CENTRAL_TAG, fmtGB, fmtMbps, quotaText, serverBandwidth } from "./central";
import { anyAction, liveStartText, liveStartTitle, serverActions } from "./serverList";

/**
 * Status card. With `children` (actions) it is a plain box whose name links to `href`; without, the whole card is the link.
 * A central recording server says so under its name (`central.line` replaces the plain tag, e.g. with the host for hub
 * administrators); `central.stats` adds rows to the stats grid and `central.details` a block above the actions (Site →
 * Servers, for those who may read the instance).
 */
export function ServerCard({ s, now, href = consoleHref(s.id), central, live, children }: {
  s: Server; now: number; href?: string; central?: { line?: string; stats?: React.ReactNode; details?: React.ReactNode }; children?: React.ReactNode;
  /** its live tiles' time to first frame (useLiveStats; admins): a "Live start" row once there are enough */
  live?: LiveStats;
}) {
  const sm = s.summary ?? {};
  const cams = sm.cameras ?? [];
  const bad = cams.filter((c) => !c.stream_ready || c.problems?.length);
  const today = Object.entries(sm.today ?? {}).map(([k, v]) => `${v} ${k}`).join(" · ");
  const diskDays = sm.disk && sm.bitrate_mbps ? Math.round(sm.disk.free_gb / ((sm.bitrate_mbps * 86400) / 8 / 1000)) : null;
  const cls = `site-card ${s.online ? "" : "offline"} ${s.retired_at ? "retired" : ""}`;
  const bw = serverBandwidth(s);
  const body = (
    <>
      <div className="head">
        <span className={`dot ${s.online ? "ok" : "bad"}`} title={s.online ? "Online" : `Offline · last seen ${ago(s.last_seen_at, now)}`} />
        {children ? <a href={href}><strong>{s.name}</strong></a> : <strong>{s.name}</strong>}
        {s.retired_at ? <span className="alert-kind">retired</span> : null}
        <span className="spacer" />
        <span className="muted small">{s.online ? "online" : `offline · ${ago(s.last_seen_at, now)}`}</span>
      </div>
      {s.central && <div className="loc central-tag">{central?.line ?? CENTRAL_TAG}</div>}
      {s.location && <div className="loc">{s.location}</div>}
      <div className="stats">
        <div><span>Cameras</span> {cams.length - bad.length}/{cams.length} up</div>
        <div><span>Today</span> {today || "—"}</div>
        <div><span>Disk</span> {sm.disk ? `${sm.disk.free_gb.toLocaleString()} GB free${diskDays != null && isFinite(diskDays) ? ` · ~${diskDays} d` : ""}` : "—"}</div>
        <div><span>AI</span> {sm.yolo_ready ? "YOLO ✓" : "YOLO …"} · {sm.vlm_ready ? "Otso ✓" : "Otso …"}{sm.queues?.synopsis ? ` (${sm.queues.synopsis} waiting)` : ""}</div>
        <div><span>Stream</span> {sm.bitrate_mbps != null ? `${sm.bitrate_mbps} Mbps` : "—"}</div>
        {bw && <div><span>Upload</span> {fmtMbps(bw.mbps)}{bw.month_gb != null ? ` · ${fmtGB(bw.month_gb)} this month` : ""}</div>}
        {live && liveStartText(live) && <div title={liveStartTitle(live)}><span>Live start</span> {liveStartText(live)}</div>}
        {s.central && <div><span>Storage</span> {quotaText(s.central.used_gb, s.central.quota_gb)}</div>}
        {s.central && central?.stats}
        <div><span>Version</span> {s.version ?? "—"}{s.clock_skew_s != null && Math.abs(s.clock_skew_s) > 30 ? ` · clock ${s.clock_skew_s > 0 ? "+" : ""}${Math.round(s.clock_skew_s)} s` : ""}</div>
      </div>
      {cams.length > 0 && (
        <div className="cams">{cams.map((c) => <span key={c.id} className={`cam ${!c.stream_ready || c.problems?.length ? "bad" : ""}`} title={(c.problems ?? []).join("; ") || (c.stream_ready ? "streaming" : "no stream")}>
          {c.name}{c.ptz && !c.ptz.at_home ? " ↗" : ""}</span>)}</div>
      )}
      {sm.site_link_down && <div className="alerts">⚠ No camera reachable: the link to the site may be down</div>}
      {s.open_alerts > 0 && <div className="alerts">⚠ {s.open_alerts} open alert{s.open_alerts > 1 ? "s" : ""}</div>}
      {central?.details}
    </>
  );
  return children ? <div className={cls}>{body}<div className="row server-actions">{children}</div></div> : <a className={cls} href={href}>{body}</a>;
}

/**
 * The live-start numbers (hub telemetry: time to first frame) of these servers, read once per set of servers; `enabled`
 * only for those the hub lets read them (the customer's admins, hub administrators). Retired servers are skipped.
 */
export function useLiveStats(servers: Pick<Server, "id" | "retired_at">[], enabled: boolean): Record<string, LiveStats> {
  const [stats, setStats] = useState<Record<string, LiveStats>>({});
  const ids = enabled ? servers.filter((s) => !s.retired_at).map((s) => s.id).sort().join(",") : "";
  useEffect(() => {
    if (!ids) return;
    let alive = true;
    for (const id of ids.split(",")) api.liveStats(id).then((st) => { if (alive) setStats((m) => ({ ...m, [id]: st })); }).catch(() => {});
    return () => { alive = false; };
  }, [ids]);
  return stats;
}

/** The admin actions' handlers, shared by the inline buttons (ServerActions) and the ⋯ menu (ServerMenu). */
function serverOps(s: Server, onChanged: () => void) {
  const act = (f: () => Promise<unknown>, done?: string) => async () => { try { await f(); if (done) toast.success(done); onChanged(); } catch (e) { toast.error(e); } };
  return {
    rename: async () => { const name = await promptDialog("Rename server", { initial: s.name, label: "Name" }); if (name?.trim()) await act(() => api.updateServer(s.id, { name: name.trim() }))(); },
    note: async () => { const loc = await promptDialog("Where is this server?", { initial: s.location, label: "Note (rack, room…)" }); if (loc != null) await act(() => api.updateServer(s.id, { location: loc.trim() }))(); },
    move: (to: Pick<Site, "id" | "name">) => act(() => api.updateServer(s.id, { location_id: to.id }), `${s.name} moved to ${to.name}`)(),
    rotate: async () => { if (await confirmDialog(`Rotate ${s.name}'s token?`)) await act(() => api.rotateServer(s.id), "New token sent to the server")(); },
    retire: async () => { if (s.retired_at || await confirmDialog(`Retire ${s.name}?`, { message: "It disappears from Sites, Home, Find, Ask and alerts. The server, its tunnel and its recordings are untouched; you can restore it under Customer → Servers.", confirmLabel: "Retire" })) await act(() => api.retireServer(s.id, !s.retired_at))(); },
    remove: async () => { if (await confirmDialog(`Remove ${s.name}?`, { message: "The server is told to unenroll; recordings stay on it.", confirmLabel: "Remove", danger: true })) await act(() => api.removeServer(s.id))(); },
  };
}
const MOVE_MESSAGE = "Its cameras, alerts and events follow it. People who can see only the old site stop seeing it.";
const ROTATE_TITLE = "Issue a new device token (the old one stops working after 10 minutes)";
const retireTitle = (s: Server) => (s.retired_at ? "Show it in Sites, Home, Find and alerts again" : "Hide it from Sites, Home, Find and alerts (the server keeps running)");

/**
 * Rename, note, token, backups, retire, remove, and Move to another Site as inline buttons (Site → Servers, the server
 * panel). `sites` = the customer's Sites for the move picker (omit to hide it). Which of them show is serverList.ts
 * serverActions: viewers get only the console link, a central recording server fewer.
 */
export function ServerActions({ s, admin, sites, onChanged }: { s: Server; admin: boolean; sites?: Pick<Site, "id" | "name">[]; onChanged: () => void }) {
  const others = (sites ?? []).filter((l) => l.id !== s.location_id);
  const can = serverActions(s, admin, others.length);
  const op = serverOps(s, onChanged);
  return (
    <>
      <a className="small" href={consoleHref(s.id)} title="The server's own interface, through its tunnel">Open server console ↗</a>
      {can.rename && <button className="ghost small" onClick={op.rename}>Rename</button>}
      {can.note && <button className="ghost small" title="A free-text note on where this box is (rack, room…)" onClick={op.note}>Note</button>}
      {can.move && (
        <select className="small" value="" aria-label="Move to site" onChange={async (e) => {
          const to = others.find((l) => l.id === e.target.value);
          if (to && await confirmDialog(`Move ${s.name} to ${to.name}?`, { message: MOVE_MESSAGE, confirmLabel: "Move" })) await op.move(to);
        }}>
          <option value="">Move to site…</option>
          {others.map((l) => <option key={l.id} value={l.id}>{l.name}</option>)}
        </select>
      )}
      {can.rotate && <button className="ghost small" title={ROTATE_TITLE} onClick={op.rotate}>Rotate token</button>}
      {can.backups && <BackupsButton server={s} />}
      {can.retire && <button className="ghost small" title={retireTitle(s)} onClick={op.retire}>{can.retire === "restore" ? "Restore" : "Retire"}</button>}
      {can.remove && <button className="ghost small" onClick={op.remove}>Remove</button>}
    </>
  );
}

/**
 * The same actions as one "Open console" link and a ⋯ menu (Customer → Servers): Rename, Note, Move to site…, Backups,
 * Restore, Rotate token, then Retire and Remove apart. Move and Backups open dialogs; the rest confirm as the buttons do.
 */
export function ServerMenu({ s, admin, sites, onChanged }: { s: Server; admin: boolean; sites?: Pick<Site, "id" | "name">[]; onChanged: () => void }) {
  const [open, setOpen] = useState(false);
  const [dialog, setDialog] = useState<"move" | "backups" | null>(null);
  const others = (sites ?? []).filter((l) => l.id !== s.location_id);
  const can = serverActions(s, admin, others.length);
  const op = serverOps(s, onChanged);
  const item = (f: () => unknown) => () => { setOpen(false); f(); };
  return (
    <div className="server-menu">
      <a className="console-link" href={consoleHref(s.id)} title="The server's own interface, through its tunnel">Open console ↗</a>
      {anyAction(can) && (
        <div className="menu-anchor">
          <button className="ghost small" aria-haspopup="true" aria-expanded={open} aria-label={`More actions for ${s.name}`} onClick={() => setOpen(!open)}>⋯</button>
          {open && (
            <>
              <div className="menu-veil" onClick={() => setOpen(false)} />
              <div className="menu-pop server-menu-pop" role="menu">
                {can.rename && <button role="menuitem" onClick={item(op.rename)}>Rename…</button>}
                {can.note && <button role="menuitem" title="A free-text note on where this box is (rack, room…)" onClick={item(op.note)}>Note…</button>}
                {can.move && <button role="menuitem" onClick={item(() => setDialog("move"))}>Move to site…</button>}
                {can.backups && <button role="menuitem" onClick={item(() => setDialog("backups"))}>Backups…</button>}
                {can.retire === "restore" && <button role="menuitem" title={retireTitle(s)} onClick={item(op.retire)}>Restore</button>}
                {can.rotate && <button role="menuitem" title={ROTATE_TITLE} onClick={item(op.rotate)}>Rotate token…</button>}
                {(can.retire === "retire" || can.remove) && <div className="menu-sep" role="separator" />}
                {can.retire === "retire" && <button role="menuitem" className="menu-danger" title={retireTitle(s)} onClick={item(op.retire)}>Retire…</button>}
                {can.remove && <button role="menuitem" className="menu-danger" onClick={item(op.remove)}>Remove…</button>}
              </div>
            </>
          )}
        </div>
      )}
      {dialog === "move" && <MoveDialog s={s} sites={others} onClose={() => setDialog(null)} onMove={(to) => { setDialog(null); op.move(to); }} />}
      {dialog === "backups" && <BackupsDialog server={s} onClose={() => setDialog(null)} />}
    </div>
  );
}

/** Move to site… from the ⋯ menu: pick the Site; the dialog says what moving does, so its Move button is the confirmation. */
function MoveDialog({ s, sites, onClose, onMove }: { s: Server; sites: Pick<Site, "id" | "name">[]; onClose: () => void; onMove: (to: Pick<Site, "id" | "name">) => void }) {
  const [to, setTo] = useState(sites.length === 1 ? sites[0].id : "");
  const site = sites.find((l) => l.id === to);
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <form className="modal central-dialog" onClick={(e) => e.stopPropagation()} style={{ maxWidth: 460 }} onSubmit={(e) => { e.preventDefault(); if (site) onMove(site); }}>
        <header className="modal-head"><h2>Move {s.name} to another site</h2><button type="button" className="ghost" onClick={onClose} aria-label="Close">✕</button></header>
        <label className="field"><span>Site</span>
          <select value={to} onChange={(e) => setTo(e.target.value)} autoFocus>
            {sites.length !== 1 && <option value="" disabled>Choose…</option>}
            {sites.map((l) => <option key={l.id} value={l.id}>{l.name}</option>)}
          </select>
        </label>
        <p className="small muted">{MOVE_MESSAGE}</p>
        <div className="row">
          <button type="submit" disabled={!site}>{site ? `Move to ${site.name}` : "Move"}</button>
          <button type="button" className="ghost" onClick={onClose}>Cancel</button>
        </div>
      </form>
    </div>
  );
}

export function BackupsButton({ server }: { server: Server }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button className="ghost small" onClick={() => setOpen(true)}>Backups</button>
      {open && <BackupsDialog server={server} onClose={() => setOpen(false)} />}
    </>
  );
}

/** A server's configuration backups: back up now, download one, restore from one. */
export function BackupsDialog({ server, onClose }: { server: Server; onClose: () => void }) {
  const [rows, setRows] = useState<Backup[]>([]);
  const load = useCallback(() => api.backups(server.id).then(setRows).catch((e) => toast.error(e)), [server.id]);
  useEffect(() => { load(); }, [load]);
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()} style={{ maxWidth: 640 }}>
        <header className="modal-head"><h2>{server.name} · configuration backups</h2><button className="ghost" onClick={onClose} aria-label="Close">✕</button></header>
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
  );
}
