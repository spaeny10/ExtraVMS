/** Organisation → Camera groups: named sets of cameras across sites, used by dashboard widgets. */
import { useEffect, useMemo, useState } from "react";
import { CameraPicker } from "@site/dashboard/CameraPicker";
import type { CameraGroup, CameraRef } from "@site/dashboard/types";
import { Icon, confirmDialog, toast } from "@site/ui";
import { api, type Fleet, type Org } from "./api";
import { makeHubSource } from "./hubSource";

export function GroupsBox({ org, fleet, canEdit }: { org: Org; fleet: Fleet | null; canEdit: boolean }) {
  const [groups, setGroups] = useState<CameraGroup[]>([]);
  const [editing, setEditing] = useState<{ id: string | null; name: string; members: CameraRef[] } | null>(null);
  const source = useMemo(() => makeHubSource(org, fleet, groups), [org, fleet, groups]);
  const load = () => api.groups(org.id).then(setGroups).catch(() => setGroups([]));
  useEffect(() => { load(); }, [org.id]); // eslint-disable-line react-hooks/exhaustive-deps
  const camName = (m: { site_id: string; camera_id: string }) => source.cameras().find((c) => c.site === m.site_id && c.id === m.camera_id)?.name ?? m.camera_id;
  const save = async () => {
    if (!editing?.name.trim()) return;
    try {
      if (editing.id) await api.updateGroup(org.id, editing.id, { name: editing.name.trim(), members: editing.members });
      else await api.createGroup(org.id, { name: editing.name.trim(), members: editing.members });
      setEditing(null); load(); toast.success("Group saved");
    } catch (e) { toast.error(e); }
  };
  return (
    <div className="card">
      <div className="row"><h3 style={{ margin: 0 }}>Camera groups</h3><span className="muted small">reusable sets of cameras across sites, for dashboard tiles and event feeds</span><span className="spacer" />
        {canEdit && <button className="ghost small" onClick={() => setEditing({ id: null, name: "", members: [] })}>New group</button>}</div>
      {groups.length === 0 && <p className="muted small" style={{ marginBottom: 0 }}>No groups yet.</p>}
      {groups.map((g) => (
        <div key={g.id} className="row" style={{ padding: "6px 0", borderBottom: "1px solid var(--border)" }}>
          <strong>{g.name}</strong>
          <span className="muted small">{g.members.length} camera{g.members.length === 1 ? "" : "s"} · {g.members.slice(0, 6).map(camName).join(", ")}{g.members.length > 6 ? "…" : ""}</span>
          <span className="spacer" />
          {canEdit && <button className="ghost small" onClick={() => setEditing({ id: g.id, name: g.name, members: g.members.map((m) => ({ site: m.site_id, camera: m.camera_id })) })}>Edit</button>}
          {canEdit && <button className="ghost small" onClick={async () => { if (await confirmDialog(`Delete group "${g.name}"?`, { confirmLabel: "Delete", danger: true })) { await api.deleteGroup(org.id, g.id); load(); } }}>Delete</button>}
        </div>
      ))}
      {editing && (
        <div className="modal-backdrop" onClick={() => setEditing(null)}>
          <div className="modal dash-settings" onClick={(e) => e.stopPropagation()}>
            <header className="modal-head"><h2>{editing.id ? "Edit group" : "New group"}</h2><button className="ghost" onClick={() => setEditing(null)} aria-label="Close"><Icon name="x" /></button></header>
            <div className="dash-settings-body">
              <label className="field"><span>Name</span><input value={editing.name} maxLength={120} autoFocus onChange={(e) => setEditing({ ...editing, name: e.target.value })} /></label>
              <CameraPicker source={source} value={editing.members} onChange={(members) => setEditing({ ...editing, members })} />
              <div className="row" style={{ justifyContent: "flex-end" }}>
                <button className="ghost" onClick={() => setEditing(null)}>Cancel</button>
                <button disabled={!editing.name.trim()} onClick={save}>Save</button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
