/** Customer → Fleet actions: what the hub's Ask box can be told to do, the safety rules, and the action log with Undo. */
import { useCallback, useEffect, useState } from "react";
import { confirmDialog, toast } from "@site/ui";
import { ActionCard } from "@site/ActionCard";
import { type ActionReference, type ExecPlan, type Org, api, fmtTime } from "../api";

/** Plan an instruction; null when it isn't one (or the planner failed), so callers can fall back to asking. */
export const planAction = (org: Org, text: string): Promise<ExecPlan | null> =>
  api.actionPlan(org.id, text).then((p) => (p.action === "none" ? null : (p as ExecPlan))).catch(() => null);

/** The confirmation card for a fleet action plan, wired to this customer's execute/undo (Find's Ask box, a Site's Alerts tab). */
export function FleetActionCard({ org, plan, onClose }: { org: Org; plan: ExecPlan; onClose: () => void }) {
  return (
    <ActionCard key={plan.id} plan={plan} helpHref="/customer/actions" onClose={onClose}
      onExecute={(x) => api.actionExecute(org.id, plan.id, x)}
      onUndo={(r) => api.actionUndo(org.id, r.audit_id!)} />
  );
}

/** Undo a fleet action from its audit row (offered for 24 h; the server runs the stored reverse plan). */
export function UndoButton({ org, id, label, onDone }: { org: Org; id: number; label: string; onDone: () => void }) {
  const [busy, setBusy] = useState(false);
  return (
    <button className="ghost small" disabled={busy} onClick={async () => {
      if (!(await confirmDialog("Undo this fleet action?", { message: label.replace(/^fleet action: /, ""), confirmLabel: "Undo" }))) return;
      setBusy(true);
      try {
        const r = await api.actionUndo(org.id, id);
        if (r.ok) toast.success(r.lines.join(" · ") || "Undone"); else toast.error(r.lines.join(" · "));
        onDone();
      } catch (e) { toast.error(e); } finally { setBusy(false); }
    }}>{busy ? "Undoing…" : "Undo"}</button>
  );
}

export function FleetActionsPage({ org }: { org: Org }) {
  const [ref, setRef] = useState<ActionReference | null>(null);
  const load = useCallback(() => api.actionReference(org.id).then(setRef).catch((e) => toast.error(e)), [org.id]);
  useEffect(() => { load(); }, [load]);
  if (!ref) return null;
  const admin = ["admin", "owner"].includes(org.role);
  return (
    <>
      <h2>Fleet actions <span className="muted small">{org.name} · what the hub's Ask box (Find → ✦ Ask all servers) can be told to do</span></h2>
      <p className="muted small">Type an instruction instead of a question and a confirmation card appears. Nothing happens until Confirm. This page is
        generated from the same list the planner reads, so every example below works as written (with your own site, server and camera names).</p>
      <div className="card">
        <h3>Safety</h3>
        <ul>{ref.safety.map((s, i) => <li key={i} className="small">{s}</li>)}</ul>
        <h3>Capacity on the card</h3>
        <ul>{ref.capacity.map((s, i) => <li key={i} className="small">{s}</li>)}</ul>
      </div>
      {ref.verbs.map((v) => (
        <div key={v.action} className="card verb-card">
          <h3>{v.title} <span className="muted small">{v.action} · {v.role}{v.confirm_name ? " · type the server name to confirm" : ""}</span></h3>
          <ul className="examples">{v.examples.map((x) => <li key={x}>"{x}"</li>)}</ul>
          <div className="verb-cols">
            <div><div className="muted small">What moves / changes</div><ul>{v.moves.map((x, i) => <li key={i} className="small">{x}</li>)}</ul></div>
            <div><div className="muted small">What stays</div><ul>{v.stays.map((x, i) => <li key={i} className="small">{x}</li>)}</ul></div>
          </div>
          <p className="small" style={{ margin: "4px 0 0" }}>
            <span className="muted">Undo ({ref.undo_hours} h): </span>{v.undo}
            {v.options.length > 0 && <><span className="muted"> · Options on the card: </span>{v.options.join(", ")}</>}
            {v.inputs.length > 0 && <><span className="muted"> · Fields on the card: </span>{v.inputs.join(", ")}</>}
          </p>
        </div>
      ))}
      <div className="card">
        <h3>Last 50 fleet actions</h3>
        {!admin && <p className="muted small">Only admins see the action log.</p>}
        {admin && ref.recent.length === 0 && <p className="muted small">None yet.</p>}
        {admin && ref.recent.length > 0 && (
          <table className="hub-table">
            <thead><tr><th>When</th><th>Who</th><th>Action</th><th>Status</th><th /></tr></thead>
            <tbody>{ref.recent.map((r) => (
              <tr key={r.id}>
                <td>{fmtTime(r.ts)}</td><td>{r.user_email ?? "—"}</td>
                <td title={r.lines.join(" · ")}>{r.action.replace(/^fleet action: /, "")}</td>
                <td>{r.status === 200 ? "done" : "failed"}</td>
                <td>{r.undo_until ? <UndoButton org={org} id={r.id} label={r.action} onDone={load} /> : null}</td>
              </tr>
            ))}</tbody>
          </table>
        )}
      </div>
    </>
  );
}
