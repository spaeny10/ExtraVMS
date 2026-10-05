/**
 * Customer → Members: one role per member plus which Sites they see. "All sites" is explicit (it also covers Sites
 * created later); with it off the member sees only the ticked Sites, and nothing when none are ticked.
 */
import { useState } from "react";
import { confirmDialog, toast } from "@site/ui";
import { type Access, type Member, type Org, type Site, ago, api } from "../api";
import { accessLabel, toggleAccess } from "../access";

export const ROLES = ["viewer", "operator", "admin", "owner"];

export function MembersBox({ org, members, sites, onChanged }: { org: Org; members: Member[]; sites: Site[]; onChanged: () => void }) {
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("viewer");
  const [password, setPassword] = useState("");
  const [access, setAccess] = useState<Access>({ all_sites: true, location_ids: [] });
  const add = async () => {
    try {
      await api.addMember(org.id, { email, role, password: password || undefined, ...access });
      setEmail(""); setPassword(""); setAccess({ all_sites: true, location_ids: [] }); onChanged(); toast.success("Member added");
    } catch (e) { toast.error(e); }
  };
  return (
    <div className="card">
      <h3>Members</h3>
      <table className="hub-table">
        <thead><tr><th>Email</th><th>Role</th><th>Sites</th><th>2FA</th><th>Last sign-in</th><th /></tr></thead>
        <tbody>{members.map((m) => (
          <tr key={m.id}>
            <td>{m.email}</td>
            <td><select value={m.role} onChange={async (e) => { try { await api.addMember(org.id, { email: m.email, role: e.target.value }); onChanged(); } catch (err) { toast.error(err); } }}>{ROLES.map((r) => <option key={r} value={r}>{r}</option>)}</select></td>
            <td><AccessPicker value={memberAccess(m)} sites={sites} onChange={async (next) => {
              try { await api.setAccess(org.id, m.id, next); onChanged(); } catch (e) { toast.error(e); }
            }} /></td>
            <td>{m.totp_enabled ? "on" : "off"}</td>
            <td>{m.last_login_at ? ago(m.last_login_at) : "never"}</td>
            <td><button className="ghost small" onClick={async () => { if (await confirmDialog(`Remove ${m.email} from ${org.name}?`, { danger: true, confirmLabel: "Remove" })) { await api.removeMember(org.id, m.id); onChanged(); } }}>Remove</button></td>
          </tr>))}
        </tbody>
      </table>
      <h4>Add a member</h4>
      <div className="row">
        <input type="email" placeholder="email" value={email} onChange={(e) => setEmail(e.target.value)} />
        <select value={role} onChange={(e) => setRole(e.target.value)}>{ROLES.map((r) => <option key={r} value={r}>{r}</option>)}</select>
        <input type="password" placeholder="initial password (new users only)" value={password} onChange={(e) => setPassword(e.target.value)} />
        <button disabled={!email} onClick={add}>Add</button>
      </div>
      <AccessPicker value={access} sites={sites} onChange={setAccess} />
      <p className="muted small">viewer: watch and search · operator: + PTZ, relay, naming, feedback, Ask · admin: + camera settings, rules, retention, members · owner: everything.</p>
    </div>
  );
}

/** A hub older than Sites sends only `sites` (server ids, [] = all): treat that as All sites rather than "none". */
const memberAccess = (m: Member): Access => ({ all_sites: m.all_sites ?? true, location_ids: m.location_ids ?? [] });

function AccessPicker({ value, sites, onChange }: { value: Access; sites: Site[]; onChange: (a: Access) => void }) {
  return (
    <div className="access-picker" title={accessLabel(value, sites)}>
      <label className="small"><input type="checkbox" checked={value.all_sites} onChange={(e) => onChange(toggleAccess(value, { all: e.target.checked }))} /> All sites</label>
      {sites.map((s) => (
        <label key={s.id} className={`small ${value.all_sites ? "muted" : ""}`}>
          <input type="checkbox" disabled={value.all_sites} checked={value.all_sites || value.location_ids.includes(s.id)}
            onChange={(e) => onChange(toggleAccess(value, { location: s.id, on: e.target.checked }))} /> {s.name}
        </label>
      ))}
      {!value.all_sites && value.location_ids.length === 0 && <span className="small" style={{ color: "var(--bad)" }}>sees no sites</span>}
    </div>
  );
}
