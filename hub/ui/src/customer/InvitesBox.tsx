/**
 * Customer → Invites: make a link (role + All sites / chosen Sites, optional email lock, label, expiry), copy it and
 * send it however you like (the hub emails nothing); pending links can be copied again or revoked. The hub caps an
 * invite like a direct grant: a Site-restricted admin gets a 403 for Sites they can't see, shown as a toast.
 */
import { useCallback, useEffect, useState } from "react";
import { confirmDialog, toast } from "@site/ui";
import { type Access, type Invite, type Me, type Org, type Site, api, fmtTime } from "../api";
import { absoluteUrl, expiresIn, inviteAccessSummary } from "../invites";
import { AccessPicker, ROLES } from "./MembersBox";

const copy = (url: string) => {
  const link = absoluteUrl(url, location.origin);
  // clipboard needs a secure context and a user gesture; when refused the link stays on screen to copy by hand
  const failed = () => toast.error("Couldn't copy: select the link and copy it");
  if (!navigator.clipboard) { failed(); return; }
  navigator.clipboard.writeText(link).then(() => toast.success("Invite link copied"), failed);
};

export function InvitesBox({ org, me, sites }: { org: Org; me: Me; sites: Site[] }) {
  const [rows, setRows] = useState<Invite[]>([]);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("viewer");
  const [access, setAccess] = useState<Access>({ all_sites: true, location_ids: [] });
  const [label, setLabel] = useState("");
  const [days, setDays] = useState(7);
  const [busy, setBusy] = useState(false);
  const [made, setMade] = useState<Invite | null>(null);
  const load = useCallback(() => api.invites(org.id).then(setRows).catch((e) => toast.error(e)), [org.id]);
  useEffect(() => { load(); }, [load]);
  // only an owner may hand out ownership (the hub refuses it otherwise)
  const roles = org.role === "owner" || me.user.is_super ? ROLES : ROLES.filter((r) => r !== "owner");
  const create = async () => {
    setBusy(true);
    try {
      const inv = await api.createInvite(org.id, { email: email.trim() || undefined, role, label: label.trim() || undefined, expires_days: days, ...access });
      // not copied here: after the await the click no longer counts as a gesture in some browsers; Copy is right below
      setMade(inv); setEmail(""); setLabel(""); load();
      toast.success("Invite link created");
    } catch (e) { toast.error(e); } finally { setBusy(false); }
  };
  const revoke = async (i: Invite) => {
    if (!(await confirmDialog("Revoke this invite?", { message: `${i.label || i.email || "The link"} stops working at once.`, confirmLabel: "Revoke", danger: true }))) return;
    try { await api.revokeInvite(org.id, i.code); if (made?.code === i.code) setMade(null); load(); } catch (e) { toast.error(e); }
  };
  return (
    <>
      <div className="card">
        <h3>Invite someone</h3>
        <p className="muted small" style={{ marginTop: 0 }}>Make a link and send it yourself (email, chat). Whoever opens it signs in or picks a password and
          joins {org.name} with the role and sites chosen here. Lock it to an email address so only that person can use it.</p>
        <div className="row invite-form">
          <label className="field"><span>Email (optional)</span><input type="email" placeholder="anyone with the link" value={email} onChange={(e) => setEmail(e.target.value)} /></label>
          <label className="field"><span>Role</span><select value={role} onChange={(e) => setRole(e.target.value)}>{roles.map((r) => <option key={r} value={r}>{r}</option>)}</select></label>
          <label className="field"><span>Label (optional)</span><input placeholder="e.g. night guard, Yard" maxLength={120} value={label} onChange={(e) => setLabel(e.target.value)} /></label>
          <label className="field"><span>Expires after (days)</span><input type="number" min={1} max={90} value={days} onChange={(e) => setDays(Math.min(90, Math.max(1, Number(e.target.value) || 7)))} style={{ width: 90 }} /></label>
          <button disabled={busy || (!access.all_sites && access.location_ids.length === 0)} onClick={create}>{busy ? "Creating…" : "Create link"}</button>
        </div>
        <AccessPicker value={access} sites={sites} onChange={setAccess} />
        {made && (
          <div className="hint-box invite-made">
            <div className="small muted">Send this link ({made.role} · {inviteAccessSummary(made, sites)} · expires {expiresIn(made.expires_at)}):</div>
            <div className="row">
              <input readOnly value={absoluteUrl(made.url, location.origin)} onFocus={(e) => e.currentTarget.select()} style={{ flex: 1, minWidth: 220 }} />
              <button className="small" onClick={() => copy(made.url)}>Copy</button>
            </div>
            <div className="small muted">Code: <code>{made.code}</code></div>
          </div>
        )}
      </div>
      <div className="card">
        <h3>Pending invites</h3>
        {rows.length === 0 ? <p className="muted small" style={{ marginBottom: 0 }}>None. Accepted and expired links drop off this list.</p> : (
          <table className="hub-table">
            <thead><tr><th>For</th><th>Role</th><th>Sites</th><th>Expires</th><th>Created by</th><th /></tr></thead>
            <tbody>{rows.map((i) => (
              <tr key={i.code}>
                <td>{i.label || i.email || <span className="muted">anyone with the link</span>}{i.label && i.email ? <div className="muted small">{i.email}</div> : null}</td>
                <td>{i.role}</td>
                <td className="small">{inviteAccessSummary(i, sites)}</td>
                <td title={fmtTime(i.expires_at)}>{expiresIn(i.expires_at)}</td>
                <td className="small">{i.created_by_email ?? "—"}{i.created_at ? <div className="muted">{fmtTime(i.created_at)}</div> : null}</td>
                <td className="row" style={{ gap: 6, flexWrap: "nowrap" }}>
                  <button className="ghost small" onClick={() => copy(i.url)}>Copy link</button>
                  <button className="ghost small" onClick={() => revoke(i)}>Revoke</button>
                </td>
              </tr>
            ))}</tbody>
          </table>
        )}
      </div>
    </>
  );
}
