/**
 * The workstation's right column: Respond (call list, procedure checklist, notes) and Resolve (dispositions).
 * Everything here writes to the incident log through the hub, so the record is what the operator did, in order.
 * Writes need the claim (the hub enforces it; the buttons say so first).
 */
import { useEffect, useRef, useState } from "react";
import type { Me, SiteContact } from "../api";
import { chordLabel } from "./chords";
import { callsByContact, flatSteps, logText, nextUncalled } from "./format";
import { socApi } from "./socApi";
import { claimedByMe, useCommand } from "./useIncident";
import type { ViewCmd } from "./IncidentView";
import { CALL_OUTCOMES, type CallOutcome, type Disposition, type DispositionGroup, type Incident, type IncidentDetail } from "./types";

type Actions = { busy: string | null; run: <T>(name: string, fn: () => Promise<T>, ok?: string) => Promise<T | null> };
export type PaneTab = "respond" | "resolve";

export function RightPane({ me, incident, detail, actions, tab, setTab, cmd, groups, autoAdvance, setAutoAdvance, onResolved }: {
  me: Me; incident: Incident; detail: IncidentDetail | null; actions: Actions; tab: PaneTab; setTab: (t: PaneTab) => void; cmd: ViewCmd | null;
  groups: DispositionGroup[]; autoAdvance?: boolean; setAutoAdvance?: (v: boolean) => void; onResolved: (i: Incident) => void;
}) {
  return (
    <div className="soc-right">
      <div className="segmented soc-pane-tabs" role="tablist" aria-label="Respond or resolve">
        <button role="tab" id="soc-tab-respond" aria-controls="soc-panel-respond" aria-selected={tab === "respond"} className={tab === "respond" ? "active" : ""} aria-keyshortcuts="G R" onClick={() => setTab("respond")}>Respond</button>
        <button role="tab" id="soc-tab-resolve" aria-controls="soc-panel-resolve" aria-selected={tab === "resolve"} className={tab === "resolve" ? "active" : ""} aria-keyshortcuts="R" onClick={() => setTab("resolve")}>Resolve</button>
      </div>
      {/* both panes stay mounted (one hidden): a chord that switches tab (F·1 from Respond) must reach a pane that
          already exists, and half-typed notes survive a look at the other tab */}
      <div role="tabpanel" id="soc-panel-respond" aria-labelledby="soc-tab-respond" hidden={tab !== "respond"}>
        <RespondPane me={me} incident={incident} detail={detail} actions={actions} cmd={cmd} />
      </div>
      <div role="tabpanel" id="soc-panel-resolve" aria-labelledby="soc-tab-resolve" hidden={tab !== "resolve"}>
        <ResolvePane me={me} incident={incident} actions={actions} cmd={cmd} groups={groups} autoAdvance={autoAdvance} setAutoAdvance={setAutoAdvance} onResolved={onResolved} />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- respond

export function RespondPane({ me, incident: i, detail, actions, cmd, phone = false }: { me: Me; incident: Incident; detail: IncidentDetail | null; actions: Actions; cmd?: ViewCmd | null; phone?: boolean }) {
  const mine = claimedByMe(i, me);
  const steps = detail ? flatSteps(detail.procedures, i.priority, detail.sop_progress ?? {}) : [];
  const tick = (n: number) => {
    const s = steps[n];
    if (!s) return;
    actions.run("sop", () => socApi.sop(i.id, s.procedureId, s.stepId, !s.done));
  };
  // digit keys (Respond tab, claimed by me) tick step N
  useCommand(cmd, (c) => { if (c.type === "sop" && c.index != null) tick(c.index); });
  if (!detail) return <p className="muted small">Loading…</p>;
  return (
    <>
      {!mine && <p className="muted small soc-claim-hint">{i.state === "new" ? "Claim the incident to log calls and steps." : i.state === "claimed" ? `${i.claimed_by_email ?? "Another operator"} has this incident: read-only here.` : "This incident is resolved: read-only."}</p>}
      <CallList incident={i} contacts={detail.contacts} log={detail.log} actions={actions} enabled={mine} phone={phone} />
      <section className="soc-sop" aria-label="Procedures">
        <h3>Procedure</h3>
        {steps.length === 0 && <p className="muted small">No procedure applies to this incident. Site → Settings → Procedures adds one.</p>}
        {steps.length > 0 && (
          <ol className="soc-steps">
            {steps.map((s, n) => (
              <li key={`${s.procedureId}:${s.stepId}`} className={s.done ? "done" : ""}>
                <label>
                  <input type="checkbox" checked={s.done} disabled={!mine || !!actions.busy} onChange={() => tick(n)} aria-keyshortcuts={s.n <= 9 ? String(s.n) : undefined} />
                  {s.n <= 9 && !phone && <kbd className="soc-kbd" aria-hidden="true">{s.n}</kbd>}
                  <span>{s.text}{s.required ? "" : <span className="muted small"> (optional)</span>}</span>
                  {s.done && s.by && <span className="muted small"> · {s.by}</span>}
                </label>
              </li>
            ))}
          </ol>
        )}
      </section>
      <NoteBox incident={i} actions={actions} enabled={mine} />
    </>
  );
}

/** The Site's call list in order; the first contact nobody has called yet is highlighted; outcomes are logged. */
export function CallList({ incident: i, contacts, log, actions, enabled, phone }: {
  incident: Incident; contacts: SiteContact[]; log: IncidentDetail["log"]; actions: Actions; enabled: boolean; phone?: boolean;
}) {
  const calls = callsByContact(log);
  const next = nextUncalled(contacts, log);
  const sorted = [...contacts].sort((a, b) => a.order - b.order);
  const log1 = (c: SiteContact, outcome: CallOutcome) => c.id != null && actions.run("call", () => socApi.call(i.id, c.id!, outcome));
  return (
    <section className="soc-calls" aria-label="Call list">
      <h3>Call list</h3>
      {sorted.length === 0 && <p className="muted small">No contacts for this Site. Site → Settings → Contacts adds them.</p>}
      <ol>
        {sorted.map((c) => (
          <li key={c.id ?? c.name} className={`soc-contact ${next && c.id === next.id ? "next" : ""}`} aria-current={next && c.id === next.id ? "step" : undefined}>
            <div className="row">
              <strong>{c.name}</strong>{c.role && <span className="muted small">{c.role}</span>}
              {next && c.id === next.id && <span className="chip small">Call next</span>}
              <span className="spacer" />
              {c.phone && <a className={phone ? "button small" : "small"} href={`tel:${c.phone.replace(/[^\d+]/g, "")}`}>{c.phone}</a>}
            </div>
            {c.notes && <div className="muted small">{c.notes}</div>}
            {(calls.get(c.id ?? -1) ?? []).map((r) => <div key={r.id} className="small soc-call-logged">✓ {logText(r, contacts).replace(/^Called [^:]+: /, "")} <span className="muted">· {new Date(r.ts * 1000).toLocaleTimeString()}{r.user_email ? ` · ${r.user_email}` : ""}</span></div>)}
            <div className="soc-outcomes" role="group" aria-label={`Outcome of calling ${c.name}`}>
              {CALL_OUTCOMES.map((o) => <button key={o.id} className="ghost small" disabled={!enabled || !!actions.busy || c.id == null} onClick={() => log1(c, o.id)}>{o.label}</button>)}
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}

function NoteBox({ incident: i, actions, enabled }: { incident: Incident; actions: Actions; enabled: boolean }) {
  const [text, setText] = useState("");
  useEffect(() => setText(""), [i.id]);
  const save = async () => {
    const t = text.trim();
    if (!t) return;
    const r = await actions.run("note", () => socApi.note(i.id, t));
    if (r !== null) setText("");
  };
  return (
    <section className="soc-notes" aria-label="Notes">
      <h3>Note</h3>
      <textarea rows={3} value={text} disabled={!enabled} placeholder={enabled ? "What you saw or did (Ctrl+Enter to log)" : "Claim the incident to add notes"}
        aria-keyshortcuts="Control+Enter" onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); save(); } }} />
      <button className="small" disabled={!enabled || !text.trim() || !!actions.busy} onClick={save}>{actions.busy === "note" ? "Logging…" : "Log note"}</button>
    </section>
  );
}

// ---------------------------------------------------------------- resolve

export const needsFourEyes = (d: Pick<Disposition, "four_eyes">, priority: string) => d.four_eyes.includes(priority as never);

/**
 * Dispositions in the catalogue's three groups, each button labelled with its chord (F·1…). A chord picks one: one
 * that needs no notes resolves at once, one that does waits here with the notes box focused. Auto-advance (default
 * on) selects the next ringing incident afterwards.
 */
export function ResolvePane({ me, incident: i, actions, cmd, groups, autoAdvance, setAutoAdvance, onResolved, phone = false }: {
  me: Me; incident: Incident; actions: Actions; cmd?: ViewCmd | null; groups: DispositionGroup[];
  autoAdvance?: boolean; setAutoAdvance?: (v: boolean) => void; onResolved?: (i: Incident) => void; phone?: boolean;
}) {
  const mine = claimedByMe(i, me);
  const [code, setCode] = useState<string | null>(null);
  const [notes, setNotes] = useState("");
  const notesRef = useRef<HTMLTextAreaElement>(null);
  useEffect(() => { setCode(null); setNotes(""); }, [i.id]);
  const all = groups.flatMap((g) => g.dispositions.map((d) => ({ g, d })));
  const chosen = all.find((x) => x.d.code === code)?.d ?? null;
  const notesMissing = !!chosen?.needs_notes && !notes.trim();
  const resolve = async (d: Disposition, n: string) => {
    const r = await actions.run("resolve", () => socApi.resolve(i.id, d.code, n.trim()),
      needsFourEyes(d, i.priority) ? `Resolved as ${d.label}: waiting for a supervisor to verify` : `Resolved as ${d.label}`);
    if (r !== null) onResolved?.(i);
  };
  useCommand(cmd, (c) => {
    if (c.type !== "disposition" || !c.code) return;
    const d = all.find((x) => x.d.code === c.code)?.d;
    if (!d) return;
    if (d.needs_notes && !notes.trim()) { setCode(d.code); requestAnimationFrame(() => notesRef.current?.focus()); }
    else resolve(d, notes);
  });

  if (!groups.length) return <p className="muted small">Loading the dispositions…</p>;
  return (
    <section className="soc-resolve" aria-label="Resolve">
      {!mine && <p className="muted small soc-claim-hint">{i.state === "claimed" ? `${i.claimed_by_email ?? "Another operator"} has this incident.` : i.state === "new" ? "Claim the incident to resolve it." : "Already resolved."}</p>}
      {groups.map((g) => (
        <fieldset key={g.id} className={`soc-dispo-group soc-dispo-${g.id}`} disabled={!mine || !!actions.busy}>
          <legend>{g.label} <kbd className="soc-kbd" aria-hidden="true">{g.key.toUpperCase()}</kbd></legend>
          <div className="soc-dispo-grid">
            {g.dispositions.filter((d) => d.selectable).map((d) => (
              <button key={d.code} className={`soc-dispo ${code === d.code ? "active" : ""}`} aria-pressed={code === d.code}
                aria-keyshortcuts={d.key && !phone ? `${g.key.toUpperCase()} ${d.key}` : undefined} onClick={() => setCode(d.code)}>
                {d.key && !phone && <kbd className="soc-kbd">{chordLabel(g.key, d.key)}</kbd>}
                <span>{d.label}</span>
                {d.needs_notes && <span className="muted small">notes</span>}
                {needsFourEyes(d, i.priority) && <span className="muted small">needs supervisor verification</span>}
              </button>
            ))}
          </div>
        </fieldset>
      ))}
      <label className="field">
        <span>Notes{chosen?.needs_notes ? " (required)" : ""}</span>
        <textarea ref={notesRef} rows={3} value={notes} disabled={!mine} aria-required={!!chosen?.needs_notes} onChange={(e) => setNotes(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && chosen && !notesMissing) { e.preventDefault(); resolve(chosen, notes); } }} />
      </label>
      {chosen && needsFourEyes(chosen, i.priority) && <p className="small soc-four-eyes">{chosen.label} on a {i.priority} priority incident needs supervisor verification: it stays open for a supervisor (not you) to verify.</p>}
      <div className="row">
        <button disabled={!mine || !chosen || notesMissing || !!actions.busy} onClick={() => chosen && resolve(chosen, notes)}
          title={!chosen ? "Pick a disposition" : notesMissing ? "This disposition needs notes" : ""}>{actions.busy === "resolve" ? "Resolving…" : chosen ? `Resolve: ${chosen.label}` : "Resolve"}</button>
        {setAutoAdvance && (
          <label className="small"><input type="checkbox" checked={!!autoAdvance} onChange={(e) => setAutoAdvance(e.target.checked)} /> Then open the next ringing incident</label>
        )}
      </div>
    </section>
  );
}
