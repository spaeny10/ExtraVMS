/** Customer → Sites: create, rename, re-address and delete Sites. A Site with servers is deleted by moving them elsewhere first. */
import { useState } from "react";
import { confirmDialog, promptDialog, toast } from "@site/ui";
import { type Org, type Site, api } from "../api";
import { ofTotal } from "../access";
import { go, siteHref } from "../nav";

export function SitesBox({ org, sites, admin, onChanged }: { org: Org; sites: Site[]; admin: boolean; onChanged: () => void }) {
  const [name, setName] = useState("");
  const [address, setAddress] = useState("");
  const [deleting, setDeleting] = useState<string | null>(null);
  const run = async (f: () => Promise<unknown>, done?: string) => { try { await f(); if (done) toast.success(done); onChanged(); return true; } catch (e) { toast.error(e); return false; } };
  const create = async () => {
    if (await run(() => api.createLocation(org.id, { name: name.trim(), address: address.trim() }), `Site ${name.trim()} created`)) { setName(""); setAddress(""); }
  };
  return (
    <div className="card">
      <h3>Sites <span className="muted small">physical places; each holds one or more servers</span></h3>
      {sites.length === 0 ? <p className="muted">None yet.</p> : (
        <table className="hub-table stack">
          <thead><tr><th>Name</th><th>Address</th><th>Servers</th><th>Cameras</th>{admin && <th />}</tr></thead>
          <tbody>{sites.map((s) => {
            const remaining = s.servers_total + s.retired_servers;
            return (
              <tr key={s.id}>
                <td className="lead"><a href={siteHref(s.id)} onClick={go(siteHref(s.id))}>{s.name}</a></td>
                <td>{s.address || <span className="muted">—</span>}</td>
                <td data-label="Servers">{ofTotal(s.servers_online, s.servers_total, "online")}{s.retired_servers ? <span className="muted small"> · {s.retired_servers} retired</span> : null}</td>
                <td data-label="Cameras">{ofTotal(s.cameras_online, s.cameras_total, "up")}</td>
                {admin && (
                  <td className="row wide">
                    <button className="ghost small" onClick={async () => { const n = await promptDialog("Rename site", { initial: s.name, label: "Name" }); if (n?.trim()) await run(() => api.updateLocation(s.id, { name: n.trim() })); }}>Rename</button>
                    <button className="ghost small" onClick={async () => { const a = await promptDialog("Site address", { initial: s.address, label: "Address" }); if (a != null) await run(() => api.updateLocation(s.id, { address: a.trim() })); }}>Address</button>
                    {deleting === s.id ? (
                      <DeleteWithMove site={s} others={sites.filter((o) => o.id !== s.id)} onCancel={() => setDeleting(null)}
                        onMove={async (to) => { if (await run(() => api.deleteLocation(s.id, to), `${s.name} deleted`)) setDeleting(null); }} />
                    ) : (
                      <button className="ghost small" onClick={async () => {
                        if (remaining > 0) { setDeleting(s.id); return; }
                        if (await confirmDialog(`Delete site ${s.name}?`, { message: "People who could see only this site lose access to it.", confirmLabel: "Delete", danger: true })) await run(() => api.deleteLocation(s.id), `${s.name} deleted`);
                      }}>Delete</button>
                    )}
                  </td>
                )}
              </tr>
            );
          })}</tbody>
        </table>
      )}
      {admin && (
        <div className="row" style={{ marginTop: 10 }}>
          <input placeholder="New site name" value={name} maxLength={120} onChange={(e) => setName(e.target.value)} />
          <input placeholder="Address (optional)" value={address} maxLength={200} onChange={(e) => setAddress(e.target.value)} style={{ flex: 1, minWidth: 180 }} />
          <button disabled={!name.trim()} onClick={create}>Create site</button>
        </div>
      )}
    </div>
  );
}

/** A Site that still has servers: pick where they go, then delete. Grants on the deleted Site are dropped, never carried over. */
function DeleteWithMove({ site, others, onMove, onCancel }: { site: Site; others: Site[]; onMove: (to: string) => void; onCancel: () => void }) {
  const [to, setTo] = useState("");
  if (others.length === 0) return <span className="muted small">Create another site to move its servers to first. <button className="ghost small" onClick={onCancel}>OK</button></span>;
  return (
    <span className="row">
      <select value={to} onChange={(e) => setTo(e.target.value)} aria-label="Move servers to">
        <option value="">Move its servers to…</option>
        {others.map((o) => <option key={o.id} value={o.id}>{o.name}</option>)}
      </select>
      <button className="small danger" disabled={!to} onClick={async () => {
        if (await confirmDialog(`Move ${site.name}'s servers and delete it?`, { message: "Who can see the servers changes to whoever can see the new site.", confirmLabel: "Move & delete", danger: true })) onMove(to);
      }}>Move &amp; delete</button>
      <button className="ghost small" onClick={onCancel}>Cancel</button>
    </span>
  );
}
