/**
 * The confirmation card for a fleet action typed into the Ask box ("Migrate Ironsight to Hailo T1"): what moves,
 * what stays, warnings and open questions, then Confirm / Cancel. Nothing happens until Confirm (admins only);
 * the result lines replace the buttons.
 */
import { useState } from "react";
import { toast } from "@site/ui";
import { type ActionPlan, type ActionResult, api } from "./api";

type Plan = Exclude<ActionPlan, { action: "none" }>;

function Lines({ title, items, tone }: { title: string; items: string[]; tone?: "warn" | "bad" }) {
  if (!items.length) return null;
  return (
    <div className={`action-lines ${tone ?? ""}`}>
      <div className="muted small">{title}</div>
      <ul>{items.map((t, i) => <li key={i}>{t}</li>)}</ul>
    </div>
  );
}

export function ActionCard({ org, plan, onClose }: { org: string; plan: Plan; onClose: () => void }) {
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ActionResult | null>(null);
  const card = plan.card;
  const canRun = plan.allowed && card.can_execute && !result;
  const confirm = async () => {
    setBusy(true);
    try { setResult(await api.actionExecute(org, plan.id)); } catch (e) { toast.error(e); } finally { setBusy(false); }
  };
  return (
    <div className="card action-card">
      <div className="row"><h3 style={{ margin: 0 }}>{card.title}</h3><span className="spacer" /><span className="muted small">fleet action{plan.parser === "ai" ? " · read by the shared AI" : ""}</span></div>
      <Lines title="Questions first" items={card.needs} tone="warn" />
      <Lines title="Can't do this now" items={card.blockers} tone="bad" />
      <Lines title="What happens" items={card.moves} />
      <Lines title="What does not move" items={card.stays} />
      <Lines title="Warnings" items={card.warnings} tone="warn" />
      {result ? (
        <Lines title={result.ok ? "Done" : "Not done"} items={result.lines} tone={result.ok ? undefined : "bad"} />
      ) : (
        <div className="row" style={{ marginTop: 10 }}>
          <button disabled={!canRun || busy} onClick={confirm}
            title={!plan.allowed ? "Only an admin of this organisation can carry out fleet actions" : !card.can_execute ? "Answer the questions above first" : ""}>
            {busy ? "Working…" : "Confirm"}
          </button>
          <button className="ghost" disabled={busy} onClick={onClose}>Cancel</button>
          {!plan.allowed && <span className="muted small">Needs the admin role.</span>}
        </div>
      )}
      {result && <div className="row" style={{ marginTop: 8 }}><button className="ghost small" onClick={onClose}>Close</button></div>}
    </div>
  );
}
