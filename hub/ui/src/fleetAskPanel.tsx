/**
 * The hub's Ask: every server's assistant answers from its own footage (fleetAsk), side by side; text that reads as a
 * hub instruction ("Migrate Ironsight to Hailo T1") gets the action card (planAction / FleetActionCard) instead.
 * Used by the customer-wide Find (FindPage) and a Site's Find tab (SiteFind, scoped to the Site).
 */
import { useState } from "react";
import { toast } from "@site/ui";
import { type ExecPlan, type Org, type ServerTag, fleetAsk } from "./api";
import { FleetActionCard, planAction } from "./customer/FleetActionsPage";
import { consoleHref } from "./nav";

export type AskAnswer = { name: string; text: string; error?: string; done?: boolean };

/** `label` names a server's answer card; `scope` (a Site id) asks only that Site's servers. */
export function useFleetAsk(org: Org, label: (t: ServerTag) => string, scope?: string) {
  const [answers, setAnswers] = useState<Record<string, AskAnswer>>({});
  const [asking, setAsking] = useState(false);
  const [action, setAction] = useState<ExecPlan | null>(null);
  const reset = () => { setAnswers({}); setAction(null); };
  const ask = async (text: string) => {
    const q = text.trim();
    if (!q) return;
    setAsking(true); setAnswers({}); setAction(null);
    // an instruction gets a confirmation card instead of going to the servers; if the planner fails the question is
    // simply asked as before
    const plan = await planAction(org, q);
    if (plan) { setAction(plan); setAsking(false); return; }
    try {
      await fleetAsk(org.id, q, (c) => {
        const tag = c as unknown as ServerTag & { site?: string };
        const id = c.site as string | undefined;
        if (c.type === "sites") {
          const init: Record<string, AskAnswer> = {};
          for (const s of c.sites as (ServerTag & { site: string })[]) init[s.site] = { name: label({ ...s, site_id: s.site }), text: "" };
          setAnswers(init);
          return;
        }
        if (!id) return;
        setAnswers((a) => {
          const cur = a[id] ?? { name: label({ ...tag, site_id: id, site_name: String(c.site_name ?? id) }), text: "" };
          if (c.type === "delta") return { ...a, [id]: { ...cur, text: cur.text + String(c.text ?? "") } };
          if (c.type === "error") return { ...a, [id]: { ...cur, error: String(c.error), done: true } };
          if (c.type === "site_done" || c.type === "done") return { ...a, [id]: { ...cur, done: true } };
          return a;
        });
      }, scope);
    } catch (e) { toast.error(e); } finally { setAsking(false); }
  };
  return { answers, asking, action, setAction, reset, ask };
}

/** The action card and the per-server answer cards. */
export function FleetAskResults({ org, answers, action, onCloseAction }: {
  org: Org; answers: Record<string, AskAnswer>; action: ExecPlan | null; onCloseAction: () => void;
}) {
  return (
    <>
      {action && <FleetActionCard org={org} plan={action} onClose={onCloseAction} />}
      {Object.keys(answers).length > 0 && (
        <div className="site-grid" style={{ marginTop: 12 }}>
          {Object.entries(answers).map(([id, a]) => (
            <div key={id} className="site-card">
              <div className="head"><strong>{a.name}</strong><span className="spacer" /><span className="muted small">{a.done ? "" : "thinking…"}</span></div>
              {a.error ? <p className="small" style={{ color: "var(--bad)" }}>{a.error}</p> : <pre style={{ whiteSpace: "pre-wrap", font: "inherit", margin: "6px 0 0" }}>{a.text || (a.done ? "No answer." : "")}</pre>}
              <a className="small" href={consoleHref(id, "find")}>Open this server's Find →</a>
            </div>
          ))}
        </div>
      )}
    </>
  );
}
