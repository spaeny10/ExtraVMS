/**
 * Site → Settings → Contacts: the ordered call list SOC operators work down during an incident (first row = first
 * call). Edited as a whole and saved with one PUT, so reordering and edits land together and the audit row shows the
 * full list. Customer admins and SOC supervisors edit it (canEditMonitoring); nobody else sees this tab.
 */
import { useCallback, useEffect, useState } from "react";
import { confirmDialog, toast } from "@site/ui";
import { type Site, type SiteContact, api } from "../api";
import { moveItem, renumber } from "./reorder";

/** The editor's row: the hub's nullable fields as "" (sent back as "", which the hub stores as NULL). */
type Row = SiteContact & { role: string; phone: string; email: string; notes: string };
const blank = (order: number): Row => ({ order, name: "", role: "", phone: "", email: "", notify_on_open: false, notes: "" });
const fromHub = (c: SiteContact[]): Row[] => [...c].sort((a, b) => a.order - b.order)
  .map((x) => ({ ...x, role: x.role ?? "", phone: x.phone ?? "", email: x.email ?? "", notes: x.notes ?? "" }));
const same = (a: Row[], b: Row[]) => JSON.stringify(renumber(a)) === JSON.stringify(renumber(b));

export function ContactsBox({ site }: { site: Site }) {
  const [saved, setSaved] = useState<Row[] | null>(null);
  const [rows, setRows] = useState<Row[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [add, setAdd] = useState<Row>(blank(0));
  const [drag, setDrag] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const load = useCallback(() => api.contacts(site.id).then((c) => { const l = fromHub(c); setSaved(l); setRows(l); setError(null); })
    .catch((e: Error) => setError(e.message.startsWith("404") ? "Contacts aren't available on this hub yet." : e.message)), [site.id]);
  useEffect(() => { load(); }, [load]);
  if (error) return <div className="card"><p className="muted" style={{ margin: 0 }}>{error}</p></div>;
  if (!saved) return <div className="card"><p className="muted" style={{ margin: 0 }}>Loading…</p></div>;

  const dirty = !same(rows, saved);
  const set = (i: number, patch: Partial<Row>) => setRows((l) => l.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const invalid = rows.some((r) => !r.name.trim() || (!r.phone.trim() && !r.email.trim()));
  const save = async () => {
    setBusy(true);
    try {
      const out = await api.setContacts(site.id, renumber(rows.map((r) => ({ ...r, name: r.name.trim(), role: r.role.trim(), phone: r.phone.trim(), email: r.email.trim(), notes: r.notes.trim() }))));
      const l = fromHub(out);
      setSaved(l); setRows(l); toast.success("Contacts saved");
    } catch (e) { toast.error(e); } finally { setBusy(false); }
  };
  const addRow = () => { setRows((l) => [...l, { ...add, order: l.length }]); setAdd(blank(0)); };
  const remove = async (i: number) => {
    if (rows[i].name.trim() && !(await confirmDialog(`Remove ${rows[i].name} from the call list?`, { confirmLabel: "Remove", danger: true }))) return;
    setRows((l) => l.filter((_, j) => j !== i));
  };

  return (
    <div className="card soc-settings">
      <h3>Call list</h3>
      <p className="muted small" style={{ marginTop: 0 }}>Who the SOC calls during an incident, in this order. Each contact needs a phone number or an email address.</p>
      {rows.length === 0 && <p className="muted">No contacts yet.</p>}
      <ol className="contact-list">
        {rows.map((r, i) => (
          <li key={r.id ?? `new-${i}`} className={`contact-row ${drag === i ? "dragging" : ""}`}
            onDragOver={(e) => { if (drag !== null && drag !== i) e.preventDefault(); }}
            onDrop={(e) => { e.preventDefault(); if (drag !== null) setRows((l) => moveItem(l, drag, i)); setDrag(null); }}>
            <span className="drag-handle" draggable title="Drag to reorder" aria-hidden="true"
              onDragStart={(e) => { setDrag(i); e.dataTransfer.effectAllowed = "move"; }} onDragEnd={() => setDrag(null)}>⠿</span>
            <span className="contact-n">{i + 1}</span>
            <div className="contact-fields">
              <input placeholder="Name" value={r.name} maxLength={120} onChange={(e) => set(i, { name: e.target.value })} />
              <input placeholder="Role (e.g. Facilities manager)" value={r.role} maxLength={80} onChange={(e) => set(i, { role: e.target.value })} />
              <input placeholder="Phone" type="tel" value={r.phone} maxLength={40} onChange={(e) => set(i, { phone: e.target.value })} />
              <input placeholder="Email" type="email" value={r.email} maxLength={200} onChange={(e) => set(i, { email: e.target.value })} />
              <input className="contact-notes" placeholder="Notes for the operator (hours, languages, gate code location…)" value={r.notes} maxLength={1000} onChange={(e) => set(i, { notes: e.target.value })} />
              <label className="small row"><input type="checkbox" checked={r.notify_on_open} onChange={(e) => set(i, { notify_on_open: e.target.checked })} /> Notify when an incident opens</label>
            </div>
            <div className="row-actions">
              <button className="ghost small" disabled={i === 0} onClick={() => setRows((l) => moveItem(l, i, i - 1))} aria-label="Move up">▲</button>
              <button className="ghost small" disabled={i === rows.length - 1} onClick={() => setRows((l) => moveItem(l, i, i + 1))} aria-label="Move down">▼</button>
              <button className="ghost small" onClick={() => remove(i)} aria-label="Remove">✕</button>
            </div>
          </li>
        ))}
      </ol>
      <form className="row contact-add" onSubmit={(e) => { e.preventDefault(); if (add.name.trim()) addRow(); }}>
        <input placeholder="Name" value={add.name} maxLength={120} onChange={(e) => setAdd({ ...add, name: e.target.value })} />
        <input placeholder="Role" value={add.role} maxLength={80} onChange={(e) => setAdd({ ...add, role: e.target.value })} />
        <input placeholder="Phone" type="tel" value={add.phone} maxLength={40} onChange={(e) => setAdd({ ...add, phone: e.target.value })} />
        <button type="submit" className="small" disabled={!add.name.trim()}>Add to list</button>
      </form>
      <p className="muted small">"Notify when an incident opens" is used once customer notifications ship; until then the SOC calls.</p>
      <div className="row">
        <button disabled={!dirty || invalid || busy} onClick={save}>Save</button>
        {dirty && <button className="ghost small" disabled={busy} onClick={() => setRows(saved)}>Discard changes</button>}
        {invalid && <span className="muted small">Every contact needs a name and a phone or email.</span>}
      </div>
    </div>
  );
}
