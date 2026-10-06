/**
 * The hub's Ask: every server's assistant answers from its own footage (fleetAsk), side by side. Questions only:
 * fleet instructions ("Migrate Ironsight to Hailo T1") are planned and run on Customer › Actions, so text that reads
 * as one (looksLikeInstruction) is not asked or planned here; a note links to the Actions page with it prefilled.
 * Used by the customer-wide Find (FindPage) and a Site's Find tab (SiteFind, scoped to the Site).
 */
import { useState } from "react";
import { toast } from "@site/ui";
import { type Org, type ServerTag, fleetAsk } from "./api";
import { actionsHref, looksLikeInstruction } from "./customer/fleetActions";
import { consoleHref, go } from "./nav";

export type AskAnswer = { name: string; text: string; error?: string; done?: boolean };

/** `label` names a server's answer card; `scope` (a Site id) asks only that Site's servers. */
export function useFleetAsk(org: Org, label: (t: ServerTag) => string, scope?: string) {
  const [answers, setAnswers] = useState<Record<string, AskAnswer>>({});
  const [asking, setAsking] = useState(false);
  /** the instruction that was typed into Ask (shown as a note linking to Customer › Actions) */
  const [instruction, setInstruction] = useState<string | null>(null);
  const reset = () => { setAnswers({}); setInstruction(null); };
  /** `anyway`: ask the servers even though it reads as an instruction (the note's "Ask anyway") */
  const ask = async (text: string, anyway = false) => {
    const q = text.trim();
    if (!q) return;
    setAnswers({}); setInstruction(null);
    if (!anyway && looksLikeInstruction(q)) { setInstruction(q); return; }
    setAsking(true);
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
  return { answers, asking, instruction, reset, ask };
}

/** "That looks like an instruction" with the link to the Actions page (prefilled, nothing run). */
export function InstructionNote({ text, onAskAnyway }: { text: string; onAskAnyway?: () => void }) {
  const href = actionsHref(text);
  return (
    <p className="small instruction-note" role="status">
      That looks like an instruction. Instructions run from <a href={href} onClick={go(href)}>Customer › Actions</a>.
      {onAskAnyway && <> <button type="button" className="ghost small" onClick={onAskAnyway}>Ask anyway</button></>}
    </p>
  );
}

/** The per-server answer cards, or the note that sends an instruction to Customer › Actions. */
export function FleetAskResults({ answers, instruction, onAskAnyway }: {
  answers: Record<string, AskAnswer>; instruction?: string | null; onAskAnyway?: () => void;
}) {
  return (
    <>
      {instruction && <InstructionNote text={instruction} onAskAnyway={onAskAnyway} />}
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
