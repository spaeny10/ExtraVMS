/**
 * The confirmation card for an instruction typed into an Ask box: the hub's fleet actions ("Migrate Ironsight to
 * Hailo T1", hub/hub/fleet_actions.py) and this site's own actions ("Lock Side Yard footage 3-4 pm today",
 * backend/nvr/site_actions.py). What changes, what stays, capacity after, warnings and open questions, then Confirm /
 * Cancel. Nothing happens until Confirm. Some actions ask for the site's name to be typed first (migrate, retire),
 * offer ticks (copy event history, skip the stream check) or fields (a new camera's password: sent with Confirm only,
 * never kept). The result lines replace the buttons, with Undo while the server still offers it.
 *
 * Shared by both UIs: the hub imports it as `@site/ActionCard`. It talks to no server itself: the page passes
 * onExecute / onUndo, so the same card drives either API.
 */
import { useState } from "react";
import { toast } from "./ui";

export type ActionOption = { key: string; label: string; default: boolean; title?: string };
export type ActionInput = { key: string; label: string; type: "password" | "text" | "number"; placeholder?: string; optional?: boolean };
export type ActionCardData = {
  title: string; moves: string[]; stays: string[]; warnings: string[]; blockers: string[]; needs: string[]; can_execute: boolean;
  capacity?: string[]; confirm_name?: string | null; options?: ActionOption[]; inputs?: ActionInput[]; undo?: string | null; role?: string;
};
export type ActionPlanCore = { id: string; action: string; parser?: string; card: ActionCardData; allowed: boolean };
export type ActionResult = { ok: boolean; lines: string[]; summary: string; audit_id?: number | null; undo_until?: number | null };
export type ActionExtras = { confirm_name?: string; options: Record<string, boolean>; inputs: Record<string, string> };

const norm = (s: string) => s.trim().replace(/\s+/g, " ").toLowerCase();

/** Why Confirm is disabled, or null when it may be pressed. Pure, so it is unit-tested (ActionCard.test.ts). */
export function blockedReason(plan: ActionPlanCore, extras: ActionExtras): string | null {
  const card = plan.card;
  if (!plan.allowed) return `Needs the ${card.role ?? "admin"} role.`;
  if (!card.can_execute) return card.needs.length ? "Answer the questions above first." : "This can't be done right now.";
  if (card.confirm_name && norm(extras.confirm_name ?? "") !== norm(card.confirm_name)) return `Type "${card.confirm_name}" to confirm.`;
  for (const i of card.inputs ?? []) if (!i.optional && !(extras.inputs[i.key] ?? "").trim()) return `Fill in ${i.label.toLowerCase()}.`;
  return null;
}

/** Is Undo still on offer for this result (server time in seconds)? */
export function canUndo(r: ActionResult | null, now = Date.now() / 1000): boolean {
  return !!r && r.ok && !!r.audit_id && !!r.undo_until && r.undo_until > now;
}

function Lines({ title, items, tone }: { title: string; items: string[] | undefined; tone?: "warn" | "bad" }) {
  if (!items?.length) return null;
  return (
    <div className={`action-lines ${tone ?? ""}`}>
      <div className="muted small">{title}</div>
      <ul>{items.map((t, i) => <li key={i}>{t}</li>)}</ul>
    </div>
  );
}

export function ActionCard({ plan, onExecute, onUndo, onClose, kind = "fleet action", helpHref }: {
  plan: ActionPlanCore;
  onExecute: (extras: ActionExtras) => Promise<ActionResult>;
  onUndo?: (r: ActionResult) => Promise<ActionResult>;
  onClose: () => void;
  kind?: string;
  helpHref?: string;
}) {
  const card = plan.card;
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ActionResult | null>(null);
  const [undone, setUndone] = useState<ActionResult | null>(null);
  const [confirmName, setConfirmName] = useState("");
  const [options, setOptions] = useState<Record<string, boolean>>(() => Object.fromEntries((card.options ?? []).map((o) => [o.key, o.default])));
  const [inputs, setInputs] = useState<Record<string, string>>({});
  const extras: ActionExtras = { confirm_name: confirmName, options, inputs };
  const blocked = blockedReason(plan, extras);
  const confirm = async () => {
    setBusy(true);
    try {
      setResult(await onExecute(extras));
      setInputs({});   // a typed password does not outlive the call
    } catch (e) { toast.error(e); } finally { setBusy(false); }
  };
  const undo = async () => {
    if (!result || !onUndo) return;
    setBusy(true);
    try { setUndone(await onUndo(result)); } catch (e) { toast.error(e); } finally { setBusy(false); }
  };
  return (
    <div className="card action-card">
      <div className="row">
        <h3 style={{ margin: 0 }}>{card.title}</h3><span className="spacer" />
        <span className="muted small">{kind}{plan.parser === "ai" ? " · read by the shared AI" : ""}</span>
        {helpHref && <a className="small" href={helpHref}>What can I ask?</a>}
      </div>
      <Lines title="Questions first" items={card.needs} tone="warn" />
      <Lines title="Can't do this now" items={card.blockers} tone="bad" />
      <Lines title="What happens" items={card.moves} />
      <Lines title="What does not change" items={card.stays} />
      <Lines title="Capacity after" items={card.capacity} />
      <Lines title="Warnings" items={card.warnings} tone="warn" />
      {result ? (
        <>
          <Lines title={result.ok ? "Done" : "Not done"} items={result.lines} tone={result.ok ? undefined : "bad"} />
          {undone && <Lines title={undone.ok ? "Undone" : "Undo failed"} items={undone.lines} tone={undone.ok ? undefined : "bad"} />}
          <div className="row" style={{ marginTop: 8 }}>
            {onUndo && !undone && canUndo(result) && (
              <button className="ghost small" disabled={busy} onClick={undo} title={card.undo ?? "Reverse this action (offered for 24 hours)"}>{busy ? "Undoing…" : "Undo"}</button>
            )}
            <button className="ghost small" onClick={onClose}>Close</button>
          </div>
        </>
      ) : (
        <>
          {(card.options?.length ?? 0) > 0 && (
            <div className="row" style={{ marginTop: 8, gap: 14 }}>
              {card.options!.map((o) => (
                <label key={o.key} className="small" title={o.title}>
                  <input type="checkbox" checked={!!options[o.key]} onChange={(e) => setOptions((v) => ({ ...v, [o.key]: e.target.checked }))} /> {o.label}
                </label>
              ))}
            </div>
          )}
          {(card.inputs?.length ?? 0) > 0 && (
            <div className="action-inputs">
              {card.inputs!.map((i) => (
                <label key={i.key} className="field">
                  <span>{i.label}{i.optional ? " (optional)" : ""}</span>
                  <input type={i.type} placeholder={i.placeholder} value={inputs[i.key] ?? ""} autoComplete={i.type === "password" ? "new-password" : "off"}
                    onChange={(e) => setInputs((v) => ({ ...v, [i.key]: e.target.value }))} />
                </label>
              ))}
            </div>
          )}
          {card.confirm_name && (
            <label className="field action-confirm">
              <span>Type <strong>{card.confirm_name}</strong> to confirm</span>
              <input value={confirmName} onChange={(e) => setConfirmName(e.target.value)} autoComplete="off" spellCheck={false} />
            </label>
          )}
          <div className="row" style={{ marginTop: 10 }}>
            <button disabled={!!blocked || busy} onClick={confirm} title={blocked ?? ""}>{busy ? "Working…" : "Confirm"}</button>
            <button className="ghost" disabled={busy} onClick={onClose}>Cancel</button>
            {blocked && <span className="muted small">{blocked}</span>}
          </div>
        </>
      )}
    </div>
  );
}
