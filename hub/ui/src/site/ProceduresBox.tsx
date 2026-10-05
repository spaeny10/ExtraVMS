/**
 * Site → Settings → Procedures: the SOPs an operator follows for an incident at this Site. Each procedure applies to
 * one kind of incident (category; "default" = any) from a minimum priority up, and is a checklist of steps; ticks are
 * written to the incident's log, and required steps must be ticked before resolving. Saved as a whole with one PUT.
 */
import { useCallback, useEffect, useState } from "react";
import { confirmDialog, toast } from "@site/ui";
import { type Procedure, type Site, api } from "../api";
import { moveItem, renumber } from "./reorder";

export const CATEGORIES = ["default", "intrusion", "loitering", "vehicle", "rule", "watched", "other"] as const;
const CATEGORY_LABEL: Record<string, string> = {
  default: "Any incident", intrusion: "Intrusion", loitering: "Loitering", vehicle: "Vehicle", rule: "Site rule broken", watched: "Watched person/vehicle", other: "Other",
};
/** "" = every priority (the hub's null). */
export const PRIORITIES = [["", "any"], ["low", "low"], ["medium", "medium"], ["high", "high"]] as const;

/**
 * The editor's procedure: category "default" stands for the hub's null (any incident), priority "" for null (any),
 * and every step has an id. Ids stay stable across edits so ticks in old incident logs still point at their step;
 * new steps get a local id ("n…", 24 chars at most like the hub's) that the hub keeps.
 */
type Step = { id: string; text: string; required: boolean };
type Row = Omit<Procedure, "category" | "priority" | "steps"> & { category: string; priority: string; steps: Step[] };
const stepId = () => `n${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
const blank = (order: number): Row => ({ order, title: "", category: "default", priority: "", steps: [{ id: stepId(), text: "", required: true }] });
const fromHub = (l: Procedure[]): Row[] => [...l].sort((a, b) => a.order - b.order).map((p) => ({
  ...p, category: p.category || "default", priority: p.priority ?? "", steps: p.steps.map((s) => ({ ...s, id: s.id || stepId() })),
}));
const clean = (l: Row[]) => renumber(l.map((p) => ({ ...p, title: p.title.trim(), steps: p.steps.map((s) => ({ ...s, text: s.text.trim() })).filter((s) => s.text) })));
const toHub = (l: Row[]): Procedure[] => clean(l).map((p) => ({
  ...p, category: p.category === "default" ? null : p.category, priority: p.priority === "" ? null : (p.priority as Procedure["priority"]),
}));

export function ProceduresBox({ site }: { site: Site }) {
  const [saved, setSaved] = useState<Row[] | null>(null);
  const [rows, setRows] = useState<Row[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const load = useCallback(() => api.procedures(site.id).then((p) => { const l = fromHub(p); setSaved(l); setRows(l); setError(null); })
    .catch((e: Error) => setError(e.message.startsWith("404") ? "Procedures aren't available on this hub yet." : e.message)), [site.id]);
  useEffect(() => { load(); }, [load]);
  if (error) return <div className="card"><p className="muted" style={{ margin: 0 }}>{error}</p></div>;
  if (!saved) return <div className="card"><p className="muted" style={{ margin: 0 }}>Loading…</p></div>;

  const dirty = JSON.stringify(clean(rows)) !== JSON.stringify(clean(saved));
  const invalid = rows.some((p) => !p.title.trim() || !p.steps.some((s) => s.text.trim()));
  const set = (i: number, patch: Partial<Row>) => setRows((l) => l.map((p, j) => (j === i ? { ...p, ...patch } : p)));
  const setSteps = (i: number, f: (s: Step[]) => Step[]) => setRows((l) => l.map((p, j) => (j === i ? { ...p, steps: f(p.steps) } : p)));
  const save = async () => {
    setBusy(true);
    try {
      const out = await api.setProcedures(site.id, toHub(rows));
      const l = fromHub(out);
      setSaved(l); setRows(l); toast.success("Procedures saved");
    } catch (e) { toast.error(e); } finally { setBusy(false); }
  };
  const remove = async (i: number) => {
    if (rows[i].title.trim() && !(await confirmDialog(`Delete the procedure "${rows[i].title}"?`, { confirmLabel: "Delete", danger: true }))) return;
    setRows((l) => l.filter((_, j) => j !== i));
    setPreview(null);
  };

  return (
    <div className="soc-settings">
      <p className="muted small" style={{ marginTop: 0 }}>The operator sees every procedure that matches the incident's kind and priority, as a checklist. Required steps must be ticked before the incident can be resolved.</p>
      {rows.length === 0 && <div className="card"><p className="muted" style={{ margin: 0 }}>No procedures yet. Operators then follow the SOC's standard response.</p></div>}
      {rows.map((p, i) => (
        <div key={p.id ?? `new-${i}`} className="card procedure">
          <div className="row procedure-head">
            <input className="procedure-title" placeholder="Title (e.g. After-hours intrusion)" value={p.title} maxLength={120} onChange={(e) => set(i, { title: e.target.value })} />
            <label className="small row">For <select value={p.category} onChange={(e) => set(i, { category: e.target.value })}>
              {CATEGORIES.map((c) => <option key={c} value={c}>{CATEGORY_LABEL[c]}</option>)}
            </select></label>
            <label className="small row">from priority <select value={p.priority} onChange={(e) => set(i, { priority: e.target.value })}>
              {PRIORITIES.map(([v, label]) => <option key={v} value={v}>{label}</option>)}
            </select></label>
            <span className="spacer" />
            <button className="ghost small" onClick={() => setPreview(preview === i ? null : i)} aria-pressed={preview === i}>{preview === i ? "Edit" : "Preview"}</button>
            <button className="ghost small" disabled={i === 0} onClick={() => setRows((l) => moveItem(l, i, i - 1))} aria-label="Move up">▲</button>
            <button className="ghost small" disabled={i === rows.length - 1} onClick={() => setRows((l) => moveItem(l, i, i + 1))} aria-label="Move down">▼</button>
            <button className="ghost small" onClick={() => remove(i)} aria-label="Delete procedure">✕</button>
          </div>
          {preview === i ? <ChecklistPreview p={p} /> : (
            <ol className="step-list">
              {p.steps.map((s, k) => (
                <li key={s.id} className="row step-row">
                  <input className="step-text" placeholder={k === 0 ? "e.g. Check every camera at the Site for people" : "Next step"} value={s.text} maxLength={300}
                    onChange={(e) => setSteps(i, (l) => l.map((x) => (x.id === s.id ? { ...x, text: e.target.value } : x)))} />
                  <label className="small row" title="Must be ticked before the incident can be resolved">
                    <input type="checkbox" checked={s.required} onChange={(e) => setSteps(i, (l) => l.map((x) => (x.id === s.id ? { ...x, required: e.target.checked } : x)))} /> required
                  </label>
                  <button className="ghost small" disabled={k === 0} onClick={() => setSteps(i, (l) => moveItem(l, k, k - 1))} aria-label="Step up">▲</button>
                  <button className="ghost small" disabled={k === p.steps.length - 1} onClick={() => setSteps(i, (l) => moveItem(l, k, k + 1))} aria-label="Step down">▼</button>
                  <button className="ghost small" onClick={() => setSteps(i, (l) => l.filter((x) => x.id !== s.id))} aria-label="Remove step">✕</button>
                </li>
              ))}
              <li><button className="ghost small" onClick={() => setSteps(i, (l) => [...l, { id: stepId(), text: "", required: false }])}>+ step</button></li>
            </ol>
          )}
        </div>
      ))}
      <div className="row">
        <button className="ghost small" onClick={() => setRows((l) => [...l, blank(l.length)])}>+ procedure</button>
        <span className="spacer" />
        {invalid && <span className="muted small">Each procedure needs a title and at least one step.</span>}
        {dirty && <button className="ghost small" disabled={busy} onClick={() => { setRows(saved); setPreview(null); }}>Discard changes</button>}
        <button disabled={!dirty || invalid || busy} onClick={save}>Save</button>
      </div>
    </div>
  );
}

/** What the operator will see: the checklist as it appears in the incident's Respond pane (ticks here do nothing). */
function ChecklistPreview({ p }: { p: Row }) {
  const steps = p.steps.filter((s) => s.text.trim());
  return (
    <div className="checklist-preview">
      <div className="small muted">{p.title || "Untitled"} · {CATEGORY_LABEL[p.category] ?? p.category} · {p.priority ? `priority ${p.priority} and up` : "any priority"}</div>
      {steps.length === 0 ? <p className="muted small">No steps yet.</p> : (
        <ul>{steps.map((s, k) => (
          <li key={s.id}><label className="row"><input type="checkbox" /> <span>{k + 1}. {s.text}</span>{s.required && <span className="chip small">required</span>}</label></li>
        ))}</ul>
      )}
    </div>
  );
}
