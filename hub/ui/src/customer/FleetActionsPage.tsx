/**
 * Customer › Actions (/customer/actions): the one place fleet instructions are planned and run.
 *  - the instruction box: type "Migrate Ironsight to Hailo T1", press Plan (Enter; Shift+Enter is a new line) and
 *    the confirmation card appears below; nothing happens until Confirm. ?text= prefills the box (Find's Ask note,
 *    a Site's "Quiet alerts…") without planning. Plans are made with origin "actions_page", the only origin the hub
 *    executes, and only for the person who made them;
 *  - the Action log: admins see the customer's last 50 fleet actions, everyone else their own, with the outcome and
 *    Undo while it is offered and theirs to use;
 *  - the reference: one line per action (click to expand); every example fills the box;
 *  - "How it's kept safe": the safety rules and the capacity notes, collapsed.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { confirmDialog, toast } from "@site/ui";
import { ActionCard } from "@site/ActionCard";
import { type ActionRecent, type ActionReference, type ActionVerb, type ExecPlan, type Org, api, fmtTime } from "../api";
import { actionText, instructionKey, outcomeText, parserLabel, textFromSearch, whereText } from "./fleetActions";

/** The confirmation card for a fleet action plan, wired to this customer's execute/undo, with who read the sentence. */
export function FleetActionCard({ org, plan, onClose, onDone }: { org: Org; plan: ExecPlan; onClose: () => void; onDone?: () => void }) {
  const read = parserLabel(plan.parser);
  return (
    <div className="fleet-action">
      {read && <p className="muted small parser-line">{read}{plan.parser === "ai" ? " (the shared AI)" : ""}</p>}
      <ActionCard key={plan.id} plan={plan} onClose={onClose}
        onExecute={async (x) => { try { return await api.actionExecute(org.id, plan.id, x); } finally { onDone?.(); } }}
        onUndo={async (r) => { try { return await api.actionUndo(org.id, r.audit_id!); } finally { onDone?.(); } }} />
    </div>
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
      } catch (e) { toast.error(e); } finally { setBusy(false); onDone(); }
    }}>{busy ? "Undoing…" : "Undo"}</button>
  );
}

function ActionLog({ org, rows, scope, onChanged }: { org: Org; rows: ActionRecent[]; scope: "all" | "own"; onChanged: () => void }) {
  return (
    <section className="card">
      <h3 style={{ marginTop: 0 }}>Action log <span className="muted small">{scope === "all" ? "last 50 fleet actions at this customer" : "your own fleet actions"}</span></h3>
      {rows.length === 0 ? <p className="muted small">None yet.</p> : (
        <table className="hub-table stack action-log">
          <thead><tr><th>When</th><th>Who</th><th>Action</th><th>Server(s) / Site</th><th>Outcome</th><th /></tr></thead>
          <tbody>{rows.map((r) => {
            const outcome = r.outcome ?? (r.status === 200 ? "done" : "failed");
            return (
              <tr key={r.id} className={outcome === "done" ? "" : `outcome-${outcome}`}>
                <td>{fmtTime(r.ts)}</td>
                <td data-label="By">{r.user_email ?? "—"}</td>
                <td className="wide" title={r.lines.join(" · ")}>{actionText(r)}</td>
                <td data-label="Where">{whereText(r)}</td>
                <td className="outcome">{outcomeText(r, fmtTime)}</td>
                <td className="acts">{r.can_undo ? <UndoButton org={org} id={r.id} label={r.action} onDone={onChanged} /> : null}</td>
              </tr>
            );
          })}</tbody>
        </table>
      )}
    </section>
  );
}

function VerbRow({ v, undoHours, onExample }: { v: ActionVerb; undoHours: number; onExample: (text: string) => void }) {
  const use = (x: string) => (e: React.MouseEvent) => { e.preventDefault(); e.stopPropagation(); onExample(x); };
  return (
    <details className="verb-row">
      <summary>
        <strong>{v.title}</strong> <span className="muted small">· {v.role}{v.confirm_name ? " · type the server name" : ""} ·</span>{" "}
        <button type="button" className="example-chip" onClick={use(v.examples[0])} title="Put this in the instruction box">"{v.examples[0]}"</button>
      </summary>
      <div className="verb-body">
        <div className="muted small">Examples (click one to put it in the box)</div>
        <ul className="examples">{v.examples.map((x) => (
          <li key={x}><button type="button" className="example-chip" onClick={use(x)}>"{x}"</button></li>
        ))}</ul>
        <div className="verb-cols">
          <div><div className="muted small">What moves / changes</div><ul>{v.moves.map((x, i) => <li key={i} className="small">{x}</li>)}</ul></div>
          <div><div className="muted small">What stays</div><ul>{v.stays.map((x, i) => <li key={i} className="small">{x}</li>)}</ul></div>
        </div>
        <p className="small" style={{ margin: "4px 0 0" }}>
          <span className="muted">Undo ({undoHours} h): </span>{v.undo}
          {v.options.length > 0 && <><span className="muted"> · Options on the card: </span>{v.options.join(", ")}</>}
          {v.inputs.length > 0 && <><span className="muted"> · Fields on the card: </span>{v.inputs.join(", ")}</>}
        </p>
      </div>
    </details>
  );
}

export function FleetActionsPage({ org }: { org: Org }) {
  const [ref, setRef] = useState<ActionReference | null>(null);
  const [text, setText] = useState(() => textFromSearch(location.search));
  const [plan, setPlan] = useState<ExecPlan | null>(null);
  const [planning, setPlanning] = useState(false);
  const [notInstruction, setNotInstruction] = useState(false);
  const box = useRef<HTMLTextAreaElement>(null);
  const load = useCallback(() => api.actionReference(org.id).then(setRef).catch((e) => toast.error(e)), [org.id]);
  useEffect(() => { load(); }, [load]);
  // ?text= prefills the box (never plans); it is dropped from the URL so a reload doesn't bring it back
  useEffect(() => {
    const take = () => {
      const t = textFromSearch(location.search);
      if (!t) return;
      setText(t); setPlan(null); setNotInstruction(false);
      history.replaceState(null, "", location.pathname);
      box.current?.focus();
    };
    take();
    addEventListener("popstate", take);
    return () => removeEventListener("popstate", take);
  }, []);

  const doPlan = async () => {
    const t = text.trim();
    if (!t || planning) return;
    setPlanning(true); setPlan(null); setNotInstruction(false);
    try {
      const p = await api.actionPlan(org.id, t);
      if (p.action === "none") setNotInstruction(true); else setPlan(p as ExecPlan);
    } catch (e) { toast.error(e); } finally { setPlanning(false); }
  };
  const fill = (example: string) => {
    setText(example); setPlan(null); setNotInstruction(false);
    box.current?.focus();
    box.current?.scrollIntoView({ block: "center", behavior: "smooth" });
  };

  return (
    <>
      <h2>Actions <span className="muted small">{org.name} · tell the hub what to do across your servers</span></h2>
      <section className="card actions-box">
        <label className="muted small" htmlFor="fleet-instruction">Instruction</label>
        <div className="actions-input">
          <textarea id="fleet-instruction" ref={box} rows={2} value={text} maxLength={500}
            placeholder={'e.g. "Quiet alerts at Main Street for 2 hours" or "Move the gate camera from Ironsight to Qwenbot"'}
            onChange={(e) => { setText(e.target.value); setNotInstruction(false); }}
            onKeyDown={(e) => { if (instructionKey({ key: e.key, shiftKey: e.shiftKey, isComposing: e.nativeEvent.isComposing }) === "plan") { e.preventDefault(); void doPlan(); } }} />
          <button onClick={() => void doPlan()} disabled={planning || !text.trim()}>{planning ? "Planning…" : "Plan"}</button>
        </div>
        <p className="muted small" style={{ margin: "6px 0 0" }}>Plan shows what would happen; nothing changes until you press Confirm on the card. Enter plans, Shift+Enter adds a line.</p>
        {notInstruction && (
          <p className="small instruction-note" role="status">That doesn't read as an instruction the hub can carry out. Pick one of the examples below and change the names, or ask questions in Find.</p>
        )}
      </section>
      {plan && <FleetActionCard org={org} plan={plan} onClose={() => setPlan(null)} onDone={load} />}
      {ref && <ActionLog org={org} rows={ref.recent} scope={ref.log_scope ?? "all"} onChanged={load} />}
      {ref && (
        <section className="card">
          <h3 style={{ marginTop: 0 }}>What you can tell the hub <span className="muted small">click an action for details; click an example to use it</span></h3>
          {ref.verbs.map((v) => <VerbRow key={v.action} v={v} undoHours={ref.undo_hours} onExample={fill} />)}
          <details className="verb-row safety">
            <summary><strong>How it's kept safe</strong></summary>
            <div className="verb-body">
              <ul>{ref.safety.map((s, i) => <li key={i} className="small">{s}</li>)}</ul>
              <div className="muted small">Capacity on the card</div>
              <ul>{ref.capacity.map((s, i) => <li key={i} className="small">{s}</li>)}</ul>
            </div>
          </details>
        </section>
      )}
    </>
  );
}
