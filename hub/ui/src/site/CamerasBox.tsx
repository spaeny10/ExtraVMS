/**
 * Site → Settings → Cameras: the Site's named camera groups and its talk-down speakers (hub camera_groups.py).
 *
 * Camera groups ("North yard", "Perimeter") hold cameras of any of the Site's servers. Everyone who sees the Site sees
 * them; its admins (and hub administrators, SOC supervisors of a monitored customer: the hub's `can_edit`) create,
 * rename, edit and delete them. Talk-down uses them now (a speaker bound to a group talks on every camera in it), Site
 * rules will later.
 * Talk-down speakers are the ones the Site's servers report (their own Settings › Cameras › Speakers adds them and binds
 * them to cameras of that server, shown read-only here); here a Site admin binds them to groups as well. The logic is
 * cameraGroups.ts.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { confirmDialog, promptDialog, toast } from "@site/ui";
import { type Camera, type Site, type SiteCameraGroup, type SiteSpeaker, api, ago } from "../api";
import { consoleHref } from "../nav";
import { type CheckServer, cameraNames, camerasByServer, membersText, toggleKeys } from "./cameraGroups";

export function CamerasBox({ site }: { site: Site }) {
  const [groups, setGroups] = useState<SiteCameraGroup[] | null>(null);
  const [canEdit, setCanEdit] = useState(false);
  const [speakers, setSpeakers] = useState<SiteSpeaker[] | null>(null);
  const [registry, setRegistry] = useState<Camera[]>([]);
  const [error, setError] = useState<string | null>(null);
  const servers = useMemo(() => site.servers.filter((s) => !s.retired_at), [site.servers]);
  const multi = servers.length > 1;
  const load = useCallback(() => Promise.all([
    api.cameraGroups(site.id).then((r) => { setGroups(r.groups); setCanEdit(r.can_edit); }),
    api.siteSpeakers(site.id).then((r) => setSpeakers(r.speakers)),
  ]).then(() => setError(null)).catch((e: Error) => setError(e.message.startsWith("404") ? "Camera groups aren't available on this hub yet." : e.message)), [site.id]);
  useEffect(() => { load(); }, [load]);
  useEffect(() => { api.locationCameras(site.id).then(setRegistry).catch(() => setRegistry([])); }, [site.id]);
  const allKeys = useMemo(() => (groups ?? []).flatMap((g) => g.cameras), [groups]);
  const checklist = useMemo(() => camerasByServer(servers, registry, allKeys), [servers, registry, allKeys]);

  if (error) return <div className="card"><p className="muted" style={{ margin: 0 }}>{error}</p></div>;
  if (!groups || !speakers) return <div className="card"><p className="muted" style={{ margin: 0 }}>Loading…</p></div>;
  return (
    <div className="soc-settings">
      <GroupsCard site={site} groups={groups} speakers={speakers} canEdit={canEdit} checklist={checklist} multi={multi} onChanged={load} />
      <SpeakersCard site={site} groups={groups} speakers={speakers} canEdit={canEdit} checklist={checklist} multi={multi} onChanged={load} />
    </div>
  );
}

type CardProps = {
  site: Site; groups: SiteCameraGroup[]; speakers: SiteSpeaker[]; canEdit: boolean; checklist: CheckServer[]; multi: boolean; onChanged: () => Promise<unknown>;
};

const speakerLabel = (s: Pick<SiteSpeaker, "name" | "server_name">, multi: boolean) => (multi ? `${s.name} (${s.server_name})` : s.name);

function GroupsCard({ site, groups, speakers, canEdit, checklist, multi, onChanged }: CardProps) {
  const [editing, setEditing] = useState<number | null>(null);
  const spName = new Map(speakers.map((s) => [`${s.server_id}/${s.id}`, speakerLabel(s, multi)]));
  const create = async () => {
    const name = (await promptDialog("New camera group", { label: "Name, e.g. North yard or Perimeter", confirmLabel: "Create" }))?.trim();
    if (!name) return;
    try {
      const g = await api.createCameraGroup(site.id, { name, cameras: [] });
      await onChanged();
      setEditing(g.id);
    } catch (e) { toast.error(e); }
  };
  const rename = async (g: SiteCameraGroup) => {
    const name = (await promptDialog("Rename camera group", { initial: g.name, confirmLabel: "Rename" }))?.trim();
    if (!name || name === g.name) return;
    try { await api.updateCameraGroup(site.id, g.id, { name }); await onChanged(); } catch (e) { toast.error(e); }
  };
  const del = async (g: SiteCameraGroup) => {
    const bound = g.speakers.length ? ` ${g.speakers.length} speaker${g.speakers.length > 1 ? "s" : ""} bound to it stop talking on its cameras.` : "";
    if (!(await confirmDialog(`Delete camera group ${g.name}?`, { message: `Its cameras are not changed.${bound}`, confirmLabel: "Delete", danger: true }))) return;
    try { await api.deleteCameraGroup(site.id, g.id); if (editing === g.id) setEditing(null); await onChanged(); } catch (e) { toast.error(e); }
  };
  return (
    <div className="card">
      <div className="row">
        <h3 style={{ margin: 0 }}>Camera groups</h3>
        <span className="spacer" />
        {canEdit && <button className="small" onClick={create}>New group</button>}
      </div>
      <p className="muted small">Named sets of this site's cameras, from any of its servers. A talk-down speaker bound to a group talks on every camera in it.</p>
      {groups.length === 0 && <p className="muted">No camera groups yet.</p>}
      <ul className="cam-groups">
        {groups.map((g) => {
          const bound = g.speakers.map((b) => spName.get(`${b.server_id}/${b.speaker_id}`)).filter(Boolean);
          return (
            <li key={g.id} className="cam-group">
              <div className="row">
                <strong>{g.name}</strong>
                <span className="muted small" title={cameraNames(g.cameras, checklist, multi).join(", ")}>{membersText(g.cameras)}</span>
                {bound.length > 0 && <span className="chip small" title="Talk-down speakers bound to this group">🔊 {bound.join(", ")}</span>}
                <span className="spacer" />
                <span className="muted small" title={g.created_by ? `Created by ${g.created_by}` : undefined}>updated {ago(g.updated_at)}</span>
                {canEdit && editing !== g.id && <button className="ghost small" onClick={() => setEditing(g.id)}>Cameras…</button>}
                {canEdit && <button className="ghost small" onClick={() => rename(g)}>Rename</button>}
                {canEdit && <button className="ghost small" onClick={() => del(g)} aria-label={`Delete ${g.name}`}>✕</button>}
              </div>
              {editing === g.id && <MembersEditor site={site} group={g} checklist={checklist} multi={multi} onDone={() => setEditing(null)} onChanged={onChanged} />}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

/** The members checklist: the Site's cameras by server, a server's "All" / "None", Save or Cancel. */
function MembersEditor({ site, group, checklist, multi, onDone, onChanged }: {
  site: Site; group: SiteCameraGroup; checklist: CheckServer[]; multi: boolean; onDone: () => void; onChanged: () => Promise<unknown>;
}) {
  const [keys, setKeys] = useState(group.cameras);
  const [busy, setBusy] = useState(false);
  const dirty = keys.length !== group.cameras.length || keys.some((k) => !group.cameras.includes(k));
  const save = async () => {
    setBusy(true);
    try { await api.updateCameraGroup(site.id, group.id, { cameras: keys }); await onChanged(); toast.success(`${group.name} saved`); onDone(); }
    catch (e) { toast.error(e); } finally { setBusy(false); }
  };
  return (
    <div className="cam-group-edit">
      {checklist.length === 0 && <p className="muted small">This site's servers haven't reported any cameras yet.</p>}
      {checklist.map((s) => {
        const all = s.cameras.map((c) => c.key);
        const on = all.filter((k) => keys.includes(k)).length;
        return (
          <fieldset key={s.id} className="cam-group-server">
            {multi || checklist.length > 1 ? (
              <legend>
                {s.name} <span className="muted small">{on} of {all.length}</span>{" "}
                <button type="button" className="ghost small" disabled={on === all.length} onClick={() => setKeys((k) => toggleKeys(k, all, true))}>All</button>
                <button type="button" className="ghost small" disabled={on === 0} onClick={() => setKeys((k) => toggleKeys(k, all, false))}>None</button>
              </legend>
            ) : null}
            <div className="cam-checks">
              {s.cameras.map((c) => (
                <label key={c.key} className={`small ${c.missing ? "muted" : ""}`} title={c.missing ? "Gone from its server" : undefined}>
                  <input type="checkbox" checked={keys.includes(c.key)} onChange={(e) => setKeys((k) => toggleKeys(k, [c.key], e.target.checked))} /> {c.name}
                </label>
              ))}
            </div>
          </fieldset>
        );
      })}
      <div className="row">
        <button disabled={!dirty || busy} onClick={save}>Save</button>
        <button className="ghost small" disabled={busy} onClick={onDone}>Cancel</button>
        <span className="muted small">{membersText(keys)}</span>
      </div>
    </div>
  );
}

function SpeakersCard({ site, groups, speakers, canEdit, checklist, multi, onChanged }: CardProps) {
  const [busy, setBusy] = useState<string | null>(null);
  const setGroups = async (s: SiteSpeaker, ids: number[]) => {
    setBusy(`${s.server_id}/${s.id}`);
    try { await api.setSpeakerGroups(site.id, s.server_id, s.id, ids); await onChanged(); } catch (e) { toast.error(e); } finally { setBusy(null); }
  };
  return (
    <div className="card">
      <h3 style={{ marginTop: 0 }}>Talk-down speakers</h3>
      <p className="muted small" style={{ marginTop: 0 }}>
        A speaker talks on the cameras bound to it on its own server (set in that server's Settings › Cameras › Speakers) and on every camera
        of the groups bound to it here. Its 🔊 Talk-down button shows on those cameras in Live.
      </p>
      {speakers.length === 0 ? <p className="muted">No speakers: add them on a server's own Settings › Cameras › Speakers.</p> : (
        <table className="hub-table stack">
          <thead><tr><th>Speaker</th><th>Its server's cameras</th><th>Camera groups</th><th>Talks on</th></tr></thead>
          <tbody>{speakers.map((s) => {
            const ref = `${s.server_id}/${s.id}`;
            const direct = cameraNames(s.camera_ids.map((c) => `${s.server_id}/${c}`), checklist, false);
            return (
              <tr key={ref}>
                <td className="lead">
                  <span className={`dot ${s.online ? "ok" : s.online === false ? "bad" : ""}`} title={s.online ? "Answered its last check" : s.online === false ? "Did not answer its last check" : "Not checked yet"} />{" "}
                  {speakerLabel(s, multi)}{s.playing && <span className="muted small"> · playing '{s.playing}'</span>}
                </td>
                <td className="small">
                  {direct.join(", ") || <span className="muted">none</span>}{" "}
                  <a className="small" href={consoleHref(s.server_id, "cameras")} title="Bind cameras on the server's own Settings">Edit ↗</a>
                </td>
                <td className="small">
                  {groups.length === 0 ? <span className="muted">no groups yet</span> : canEdit ? (
                    <div className="cam-checks">
                      {groups.map((g) => (
                        <label key={g.id}>
                          <input type="checkbox" disabled={busy === ref} checked={s.group_ids.includes(g.id)}
                            onChange={(e) => setGroups(s, e.target.checked ? [...s.group_ids, g.id] : s.group_ids.filter((x) => x !== g.id))} /> {g.name}
                        </label>
                      ))}
                    </div>
                  ) : (groups.filter((g) => s.group_ids.includes(g.id)).map((g) => g.name).join(", ") || <span className="muted">none</span>)}
                </td>
                <td className="small" title={cameraNames(s.cameras, checklist, multi).join(", ")}>{membersText(s.cameras)}</td>
              </tr>
            );
          })}</tbody>
        </table>
      )}
    </div>
  );
}
