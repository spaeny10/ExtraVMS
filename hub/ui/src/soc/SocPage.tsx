/**
 * The operator workstation (/soc): queue | incident | respond-and-resolve, each column scrolling on its own so the
 * queue stays in sight while the operator works an incident. The open incident is kept in ?incident= (replaced, not
 * pushed: Back leaves the console rather than stepping through every incident looked at).
 *
 * Keyboard: soc/chords.ts turns keys into commands (the help overlay, `?`, lists them); this page runs them.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { confirmDialog, toast } from "@site/ui";
import type { Me } from "../api";
import { incidentHref } from "../nav";
import { type ChordContext, type ChordState, type Command, IDLE, chordReducer, ignoreTarget } from "./chords";
import { HelpOverlay } from "./HelpOverlay";
import { IncidentView, type ViewCmd } from "./IncidentView";
import { QueuePane, rowId } from "./QueuePane";
import { type QueueFilters, NO_FILTERS, applyFilters, nextRinging, queueCounts, splitLanes, stepId } from "./queue";
import { type PaneTab, RightPane } from "./RightPane";
import { ringer } from "./ringer";
import { socApi } from "./socApi";
import { claimedByMe, useIncident, useIncidentActions, useNow } from "./useIncident";
import { useSoc } from "./useSocStream";
import type { DispositionGroup, Incident } from "./types";

const FILTERS_KEY = "soc.filters";
const ADVANCE_KEY = "soc.autoAdvance";
const store = {
  get: (k: string) => { try { return localStorage.getItem(k); } catch { return null; } },
  set: (k: string, v: string) => { try { localStorage.setItem(k, v); } catch { /* private mode: not remembered */ } },
};
function loadFilters(): QueueFilters {
  try { return { ...NO_FILTERS, ...JSON.parse(store.get(FILTERS_KEY) ?? "{}"), text: "" }; } catch { return NO_FILTERS; }
}
const incidentParam = () => { const n = Number(new URLSearchParams(location.search).get("incident")); return Number.isFinite(n) && n > 0 ? n : null; };

/** The false-alarm disposition code from the catalog (the sweep's "False alarm" button), "false_alarm" if absent. */
export const falseAlarmCode = (groups: DispositionGroup[]) =>
  groups.find((g) => g.id === "false_alarm")?.dispositions.find((d) => d.selectable)?.code ?? "false_alarm";

export const popOut = (id: number) => window.open(incidentHref(id), `soc-incident-${id}`, "popup,width=1280,height=900");

/**
 * Window keydown → chordReducer → run. `ctx` is read through a ref so the listener is installed once. Keys typed in
 * fields, dialogs and the Timeline are left alone (chords.ignoreTarget); Enter on a focused button stays a click.
 */
export function useChordKeys(ctx: () => ChordContext, run: (c: Command) => void, onPending?: (s: ChordState) => void) {
  const state = useRef<ChordState>(IDLE);
  const live = useRef({ ctx, run, onPending });
  live.current = { ctx, run, onPending };
  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.isComposing) return;
      const t = e.target as HTMLElement | null;
      if (ignoreTarget(t)) return;
      if (e.key === "Enter" && t?.closest?.("button, a, summary, [role=tab]")) return;
      const r = chordReducer(state.current, { key: e.key, shift: e.shiftKey, ctrl: e.ctrlKey, alt: e.altKey, meta: e.metaKey, at: performance.now() }, live.current.ctx());
      state.current = r.state;
      live.current.onPending?.(r.state);
      if (r.handled) e.preventDefault();
      if (r.command) live.current.run(r.command);
    };
    addEventListener("keydown", on);
    return () => removeEventListener("keydown", on);
  }, []);
}

/** "F… then a digit" while a chord waits for its second key (cleared when it lands or times out). */
export function useChordHint() {
  const [hint, setHint] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const onPending = useCallback((s: ChordState) => {
    if (timer.current) clearTimeout(timer.current);
    if (s.mode === "idle") { setHint(null); return; }
    setHint(s.mode === "go" ? "g… then l (live), t (timeline), r (respond) or d (details)" : `${s.key.toUpperCase()}… then a digit`);
    timer.current = setTimeout(() => setHint(null), 2500);
  }, []);
  return { hint, onPending };
}

export function SocPage({ me }: { me: Me }) {
  const soc = useSoc();
  const now = useNow(1000);
  const [filters, setFiltersState] = useState<QueueFilters>(loadFilters);
  const setFilters = (f: QueueFilters) => { setFiltersState(f); store.set(FILTERS_KEY, JSON.stringify({ ...f, text: "" })); };
  const [selected, setSelected] = useState<number | null>(incidentParam);
  const [cursor, setCursor] = useState<number | null>(selected);
  const [tab, setTab] = useState<PaneTab>("respond");
  const [cmd, setCmd] = useState<ViewCmd | null>(null);
  const issue = (type: string, extra: Partial<ViewCmd> = {}) => setCmd({ type, nonce: Date.now() + Math.random(), ...extra });
  const [help, setHelp] = useState(false);
  const [autoAdvance, setAutoAdvanceState] = useState(() => store.get(ADVANCE_KEY) !== "0");
  const setAutoAdvance = (v: boolean) => { setAutoAdvanceState(v); store.set(ADVANCE_KEY, v ? "1" : "0"); };
  const filterRef = useRef<HTMLInputElement>(null);
  const { hint, onPending } = useChordHint();

  const lanes = useMemo(() => splitLanes(soc.queue.incidents), [soc.queue.incidents]);
  const ringing = applyFilters(lanes.ring, filters, me.user.id);
  // the quiet lane takes the customer, priority and text filters; Mine / Unclaimed are about the ringing work
  const quiet = applyFilters(lanes.quiet, { ...filters, mine: false, unclaimed: false }, me.user.id);
  const counts = queueCounts(soc.queue.incidents, me.user.id);
  const customers = useMemo(() => {
    const m = new Map<string, string>();
    for (const i of soc.queue.incidents) m.set(i.org_id, i.org_name);
    return [...m.entries()].map(([id, name]) => ({ id, name })).sort((a, b) => a.name.localeCompare(b.name));
  }, [soc.queue.incidents]);

  useEffect(() => {
    const q = new URLSearchParams(location.search);
    if (selected) q.set("incident", String(selected)); else q.delete("incident");
    const s = q.toString();
    history.replaceState(history.state, "", `${location.pathname}${s ? `?${s}` : ""}${location.hash}`);
  }, [selected]);

  const { detail, incident, error, reload } = useIncident(selected);
  const actions = useIncidentActions(reload);
  const mine = claimedByMe(incident, me);

  const open = (id: number) => {
    setSelected(id); setCursor(id); setTab("respond");
    // focus the incident's heading so screen readers land on what just opened
    requestAnimationFrame(() => document.getElementById("soc-incident-title")?.focus());
  };
  const onResolved = (i: Incident) => {
    if (!autoAdvance) return;
    const n = nextRinging(soc.queue.incidents, i.id);
    if (n) open(n.id);
  };
  const claimNext = async () => {
    const n = nextRinging(lanes.ring);
    if (!n) { toast.info("Nothing unclaimed is ringing"); return; }
    open(n.id);
    await actions.run("claim", () => socApi.claim(n.id));
  };
  const sweepFalse = (i: Incident) => actions.run("sweep", async () => {
    // the hub resolves only claimed incidents (owner or supervisor): claim the quiet one first, then close it
    if (i.state === "new") await socApi.claim(i.id);
    return socApi.resolve(i.id, falseAlarmCode(soc.groups), "");
  }, "Resolved as false alarm");
  const promote = (i: Incident) => actions.run("promote", () => socApi.promote(i.id), "Moved to the ringing lane");
  const sweepSite = async (loc: string, name: string) => {
    if (!(await confirmDialog(`Sweep the quiet lane at ${name}?`, { message: "Every low-priority incident there is closed as swept.", confirmLabel: "Sweep" }))) return;
    await actions.run("sweep", () => socApi.sweep(loc), `Swept ${name}`);
    soc.refresh();
  };

  const run = (c: Command) => {
    switch (c.type) {
      case "next": case "prev": {
        const n = stepId(ringing.map((i) => i.id), cursor ?? selected, c.type === "next" ? 1 : -1);
        setCursor(n);
        if (n != null) document.getElementById(rowId(n))?.scrollIntoView({ block: "nearest" });
        return;
      }
      case "open": if (cursor != null) open(cursor); return;
      case "claim":
        if (incident?.state === "new") actions.run("claim", () => socApi.claim(incident.id));
        else toast.info(incident?.claimed_by_email ? `${incident.claimed_by_email} has this incident` : "Not claimable");
        return;
      case "claimNext": claimNext(); return;
      case "release": if (incident) actions.run("release", () => socApi.release(incident.id), "Released to the queue"); return;
      case "handoff": case "goLive": case "goTimeline": case "goDetails": issue(c.type); return;
      case "resolveTab": setTab("resolve"); return;
      case "goRespond": setTab("respond"); return;
      case "mute": ringer().toggleMute(); return;
      case "popout": if (selected) popOut(selected); return;
      case "filter": filterRef.current?.focus(); filterRef.current?.select(); return;
      case "help": setHelp(true); return;
      case "escape": setHelp(false); (document.activeElement as HTMLElement | null)?.blur?.(); return;
      case "disposition": setTab("resolve"); issue("disposition", { code: c.code }); return;
      case "sop": issue("sop", { index: c.index }); return;
      case "refuse": toast.info(c.reason); return;
    }
  };
  useChordKeys(() => ({ tab, hasIncident: !!incident, claimedByMe: mine, groups: soc.groups }), run, onPending);

  return (
    <>
      <div className="soc-sr" aria-live="polite" role="status">{soc.announcement}</div>
      {soc.escalated.length > 0 && (
        <div className="soc-escalation" role="alert">
          <strong>⚠ Escalated to supervisors:</strong>
          {soc.escalated.map((i) => <button key={i.id} className="small" onClick={() => open(i.id)}>#{i.id} {i.org_name} › {i.location_name}{i.claimed_by_email ? ` (${i.claimed_by_email})` : " (unclaimed)"}</button>)}
        </div>
      )}
      {hint && <div className="soc-chord-hint" role="status">{hint}</div>}
      <div className="soc-work">
        <aside className="soc-col soc-col-queue" aria-label="Queue">
          <QueuePane ref={filterRef} ringing={ringing} quiet={quiet} totalRinging={counts.ringing} unclaimed={counts.unclaimed} mine={counts.mine}
            filters={filters} setFilters={setFilters} customers={customers} selected={selected} cursor={cursor} onOpen={open} now={now} meId={me.user.id}
            onStep={(d) => run({ type: d === 1 ? "next" : "prev" })}
            onSweepFalse={sweepFalse} onPromote={promote} onSweepSite={sweepSite} busy={actions.busy} />
        </aside>
        <section className="soc-col soc-col-main" aria-label="Incident">
          {incident ? <IncidentView me={me} detail={detail} incident={incident} now={now} actions={actions} cmd={cmd} />
            : error ? <p className="muted">{error}</p>
            : selected ? <p className="muted">Loading incident #{selected}…</p>
            : <div className="soc-empty muted">
                <p>Pick an incident from the queue (<kbd>j</kbd>/<kbd>k</kbd>, <kbd>Enter</kbd>), or <kbd>Shift</kbd>+<kbd>C</kbd> to claim the next ringing one.</p>
                <p className="small"><kbd>?</kbd> lists every key.</p>
              </div>}
        </section>
        <aside className="soc-col soc-col-right" aria-label="Respond and resolve">
          {incident ? <RightPane me={me} incident={incident} detail={detail} actions={actions} tab={tab} setTab={setTab} cmd={cmd} groups={soc.groups}
            autoAdvance={autoAdvance} setAutoAdvance={setAutoAdvance} onResolved={onResolved} />
            : <p className="muted small">Call list, procedures and dispositions appear here for the open incident.</p>}
        </aside>
      </div>
      {help && <HelpOverlay groups={soc.groups} onClose={() => setHelp(false)} />}
    </>
  );
}
