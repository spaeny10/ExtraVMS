/**
 * The supervisor view (/soc/supervisor): how the queue is doing (tiles), who is working what (operators board), every
 * open incident across customers, arming overrides for monitored Sites, the shift's resolutions with four-eyes
 * verification, and the SLA policy.
 *
 * Freshness: tiles, the board's current incidents and the open table read the live queue the SOC socket keeps
 * (useSoc), so they move the instant a frame lands. The overview (per-operator counts the queue can't know, such as
 * resolved in 24 h), the resolved list and the Sites are REST: every 15 s, and again shortly after any incident frame
 * (debounced, so a burst of frames costs one request) or arming frame.
 *
 * Phones get the parts a supervisor checks on the move: tiles, operators, today's resolved.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { confirmDialog, errorText, promptDialog, toast, useIsPhone } from "@site/ui";
import { type Me, api } from "../api";
import { isSocSupervisor } from "../access";
import { go, incidentHref, navigate, socHref } from "../nav";
import { OVERRIDE_DURATIONS, type OverrideDuration, REASON_LABEL, fmtInZone } from "../site/monitoring";
import { fmtDur, incidentTitle, parseSeconds, shiftStart, STATE_LABEL } from "./format";
import { IncidentPage } from "./IncidentPage";
import { SlaPill } from "./IncidentView";
import { ringer } from "./ringer";
import { clock, priorityClass, priorityLabel } from "./sla";
import { socApi, socSupervisorApi } from "./socApi";
import {
  type BoardRow, type OpenFilters, type ResolvedRow, type SlaDraft, NO_OPEN_FILTERS, PRIORITIES, chimeDue, customersOf, mergeRoster, openIncidents, operatorBoard, withLive,
  queueHealth, resolvedSince, slaDraft, slaPatch,
} from "./supervisor";
import { useIncidentActions, useNow } from "./useIncident";
import { useSoc } from "./useSocStream";
import type { Incident, Overview, Presence, Priority, SlaPolicy, SocSite } from "./types";

const POLL_MS = 15000;

/** Re-run `fn` every `ms`, and `delay` ms after `key` changes (a burst of changes runs it once). */
function usePoll(fn: () => void, ms: number, key: unknown, delay = 800) {
  const latest = useRef(fn);
  latest.current = fn;
  useEffect(() => { latest.current(); const t = setInterval(() => latest.current(), ms); return () => clearInterval(t); }, [ms]);
  const first = useRef(true);
  useEffect(() => {
    if (first.current) { first.current = false; return; }
    const t = setTimeout(() => latest.current(), delay);
    return () => clearTimeout(t);
  }, [key, delay]);
}

function useMedia(q: string): boolean {
  const [m, setM] = useState(() => typeof matchMedia !== "undefined" && matchMedia(q).matches);
  useEffect(() => { const mq = matchMedia(q); const on = () => setM(mq.matches); mq.addEventListener("change", on); return () => mq.removeEventListener("change", on); }, [q]);
  return m;
}

export function SupervisorPage({ me }: { me: Me }) {
  if (!isSocSupervisor(me)) {
    return (
      <div className="card soc-placeholder">
        <h2>Supervisors only</h2>
        <p className="muted">The supervisor view is for SOC supervisors. <a href={socHref()} onClick={go(socHref())}>Back to the queue</a></p>
      </div>
    );
  }
  return <Supervisor me={me} />;
}

function Supervisor({ me }: { me: Me }) {
  const soc = useSoc();
  const now = useNow(1000);
  const phone = useIsPhone();
  // the incident opens beside the tables on a wide screen; below that a drawer would cover them, so navigate
  const narrow = useMedia("(max-width: 1100px)");
  const [panel, setPanel] = useState<number | null>(null);
  const openIncident = (id: number) => { if (narrow || phone) navigate(incidentHref(id)); else setPanel(id); };

  const [overview, setOverview] = useState<Overview | null>(null);
  const loadOverview = useCallback(() => socSupervisorApi.overview().then(setOverview).catch(() => {}), []);
  usePoll(loadOverview, POLL_MS, soc.queue.rev);

  // resolutions since the shift began. The hub's `since` filters on opening time, so ask from a day earlier and keep
  // the ones resolved since the handover (an incident opened before it but resolved after belongs to this shift)
  const [resolved, setResolved] = useState<Incident[]>([]);
  const start = shiftStart(now);
  const loadResolved = useCallback(() => {
    const since = shiftStart(Date.now() / 1000);
    socApi.incidents({ state: "closed,pending_verify", since: Math.floor(since - 86400), limit: 1000 }).then(setResolved).catch(() => {});
  }, []);
  usePoll(loadResolved, POLL_MS, `${soc.queue.rev}:${start}`);

  const [sites, setSites] = useState<SocSite[] | null>(null);
  const loadSites = useCallback(() => socApi.sites().then(setSites).catch(() => {}), []);
  usePoll(loadSites, 30000, JSON.stringify(soc.queue.arming), 300);

  const refreshAll = useCallback(() => { loadOverview(); loadResolved(); soc.refresh(); }, [loadOverview, loadResolved, soc]);
  const actions = useIncidentActions(refreshAll);

  // the escalation chime: the provider chimes when a frame crosses level 2; incidents already escalated when this
  // page opened (they came in the snapshot) chime here, once each (the ringer remembers which have had theirs)
  useEffect(() => { for (const id of chimeDue(soc.escalated, ringer().chimedIds)) ringer().chimeOnce(id); }, [soc.escalated]);

  // the socket knows first when a resolution is verified or sent back: its rows win over the last REST answer
  const resolvedNow = useMemo(() => withLive(resolved, soc.queue.incidents), [resolved, soc.queue.incidents]);
  const health = useMemo(() => queueHealth(soc.queue.incidents, now), [soc.queue.incidents, now]);
  const roster = useMemo(() => mergeRoster(soc.queue.presence, overview?.operators), [soc.queue.presence, overview]);
  const board = useMemo(() => operatorBoard(roster, soc.queue.incidents, now, soc.sla), [roster, soc.queue.incidents, now, soc.sla]);
  const label = useMemo(() => {
    const m = new Map(soc.groups.flatMap((g) => g.dispositions.map((d) => [d.code, d.label] as const)));
    return (code: string | null) => (code ? m.get(code) ?? code.replace(/_/g, " ") : "—");
  }, [soc.groups]);
  const emailOf = useMemo(() => {
    const m = new Map(roster.map((p) => [p.user_id, p.email]));
    return (id: string | null, fallback?: string | null) => (id ? m.get(id) ?? fallback ?? "someone" : "—");
  }, [roster]);

  return (
    <div className="soc-sup">
      {soc.escalated.length > 0 && (
        <div className="soc-escalation" role="alert">
          <strong>⚠ Escalated to supervisors:</strong>
          {soc.escalated.map((i) => (
            <button key={i.id} className="small" onClick={() => openIncident(i.id)}>
              #{i.id} {i.org_name} › {i.location_name} · L{i.escalation_level}{i.claimed_by_email ? ` (${i.claimed_by_email})` : " (unclaimed)"}
            </button>
          ))}
        </div>
      )}
      <Tiles health={health} overview={overview} />
      <div className={`soc-sup-body ${panel ? "with-panel" : ""}`}>
        <div className="soc-sup-main">
          <OperatorsBoard rows={board} roster={roster} me={me} actions={actions} onOpen={openIncident} />
          {!phone && <OpenIncidents incidents={soc.queue.incidents} roster={roster} now={now} onOpen={openIncident} onDone={refreshAll} />}
          {!phone && <ArmingPanel sites={sites} reload={loadSites} now={now} />}
          <ResolvedList rows={resolvedSince(resolvedNow, start, me.user.id)} since={start} label={label} emailOf={emailOf} actions={actions} onOpen={openIncident} compact={phone} />
          {!phone && <SlaEditor />}
        </div>
        {panel != null && !narrow && !phone && (
          <aside className="soc-sup-panel" aria-label={`Incident #${panel}`}>
            <div className="row soc-sup-panel-head">
              <strong>Incident #{panel}</strong>
              <a className="small" href={incidentHref(panel)} onClick={go(incidentHref(panel))}>Full page</a>
              <button className="ghost small" onClick={() => setPanel(null)} aria-label="Close the incident panel">✕</button>
            </div>
            <IncidentPage key={panel} me={me} id={panel} />
          </aside>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- tiles

function Tiles({ health: h, overview }: { health: ReturnType<typeof queueHealth>; overview: Overview | null }) {
  const esc = overview?.escalations;
  const escTitle = esc ? `Escalated now: L1 ${esc.level1 ?? 0} · L2 ${esc.level2 ?? 0} · L3 ${esc.level3 ?? 0}` : undefined;
  const tile = (name: string, value: string | number, hot: boolean, title?: string) => (
    <div className={`soc-tile ${hot ? "hot" : ""}`} title={title} role="group" aria-label={`${name}: ${value}`}>
      <span className="soc-tile-value">{value}</span>
      <span className="soc-tile-name">{name}{hot ? " ⚠" : ""}</span>
    </div>
  );
  return (
    <div className="soc-tiles">
      {tile("Ringing", h.ringing, false, "Unclaimed in the ringing lane")}
      {tile("Oldest unclaimed", h.oldestUnclaimedS == null ? "—" : fmtDur(h.oldestUnclaimedS), h.breaches > 0)}
      {tile("SLA breaches", h.breaches, h.breaches > 0, "Unclaimed past the time to claim")}
      {tile("Overdue", h.overdue, h.overdue > 0, "Claimed, past the time to resolve")}
      {tile("Quiet", h.quiet, false, "Low priority, waiting for a sweep")}
      {tile("To verify", h.pendingVerify, false, escTitle ?? "Resolutions waiting for a second supervisor")}
    </div>
  );
}

// ---------------------------------------------------------------- operators board

type Actions = ReturnType<typeof useIncidentActions>;
const STATUS_LABEL: Record<string, string> = { available: "Available", engaged: "Engaged", break: "On break", offline: "Away" };

function OperatorsBoard({ rows, roster, me, actions, onOpen }: { rows: BoardRow[]; roster: Presence[]; me: Me; actions: Actions; onOpen: (id: number) => void }) {
  const [handing, setHanding] = useState<number | null>(null);
  const takeover = async (i: Incident) => {
    if (!(await confirmDialog(`Take over incident #${i.id} from ${i.claimed_by_email ?? "its operator"}?`, { message: "They lose the claim and are told so in the log.", confirmLabel: "Take over", danger: true }))) return;
    actions.run("takeover", () => socApi.takeover(i.id), `You have #${i.id} now`);
  };
  const handoff = (i: Incident, to: string) => {
    setHanding(null);
    const p = roster.find((x) => x.user_id === to);
    actions.run("handoff", () => socApi.handoff(i.id, to), `#${i.id} handed to ${p?.email ?? "them"}`);
  };
  return (
    <section className="card soc-sup-card">
      <h3>Operators</h3>
      {rows.length === 0 ? <p className="muted small">Nobody in the SOC has signed in yet.</p> : (
        <table className="hub-table soc-board">
          <thead><tr><th>Operator</th><th>Status</th><th>Working</th><th>On it</th><th title="Claims held">Held</th><th title="Resolved, awaiting verification">To verify</th><th>Resolved 24 h</th><th /></tr></thead>
          <tbody>
            {rows.map((r) => {
              const i = r.incident;
              const theirs = !!i && i.state === "claimed" && i.claimed_by === r.operator.user_id;
              return (
                <tr key={r.operator.user_id} className={r.status === "offline" ? "muted" : ""}>
                  <td><span className={`dot soc-dot-${r.status}`} aria-hidden /> {r.operator.email}{r.operator.soc_role === "supervisor" ? <span className="muted small"> · sup</span> : null}</td>
                  <td>{STATUS_LABEL[r.status] ?? r.status}</td>
                  <td>{i ? <button className="soc-linklike" onClick={() => onOpen(i.id)} title={`${i.org_name} › ${i.location_name}`}>#{i.id} {i.location_name}</button> : <span className="muted">—</span>}</td>
                  <td>{r.onItS == null ? "—" : <span className={`soc-sla ${r.overResolve ? "breach" : "ok"}`} title={r.overResolve ? "Past the time to resolve" : undefined}>{r.overResolve ? "⚠ " : ""}{clock(r.onItS)}</span>}</td>
                  <td>{r.claimed}</td>
                  <td>{r.pendingVerify}</td>
                  <td>{r.resolved24h}</td>
                  <td className="soc-board-actions">
                    {theirs && r.operator.user_id !== me.user.id && <button className="ghost small" disabled={!!actions.busy} onClick={() => takeover(i!)}>Take over</button>}
                    {theirs && (handing === i!.id ? (
                      <select autoFocus aria-label={`Hand #${i!.id} off to`} defaultValue="" onChange={(e) => e.target.value && handoff(i!, e.target.value)} onBlur={() => setHanding(null)}>
                        <option value="" disabled>Hand off to…</option>
                        {roster.filter((p) => p.user_id !== r.operator.user_id && p.status !== "offline" && p.on_shift !== false).map((p) => <option key={p.user_id} value={p.user_id}>{p.email} ({STATUS_LABEL[p.status] ?? p.status})</option>)}
                      </select>
                    ) : <button className="ghost small" disabled={!!actions.busy} onClick={() => setHanding(i!.id)}>Hand off…</button>)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </section>
  );
}

// ---------------------------------------------------------------- open incidents across customers

function OpenIncidents({ incidents, roster, now, onOpen, onDone }: { incidents: Incident[]; roster: Presence[]; now: number; onOpen: (id: number) => void; onDone: () => void }) {
  const [f, setF] = useState<OpenFilters>(NO_OPEN_FILTERS);
  const [picked, setPicked] = useState<Set<number>>(new Set());
  const [busy, setBusy] = useState(false);
  const rows = openIncidents(incidents, f);
  const customers = customersOf(incidents);
  // only active incidents can be handed off (awaiting verification is the supervisors' own)
  const handable = rows.filter((i) => i.state === "new" || i.state === "claimed");
  const chosen = handable.filter((i) => picked.has(i.id));
  const toggle = (id: number, on: boolean) => setPicked((s) => { const n = new Set(s); if (on) n.add(id); else n.delete(id); return n; });
  const bulk = async (to: string) => {
    const who = roster.find((p) => p.user_id === to)?.email ?? "them";
    setBusy(true);
    const failed: string[] = [];
    // one at a time: each is a conditional update on the hub, and a 409 on one must not stop the rest
    for (const i of chosen) {
      try { await socApi.handoff(i.id, to); } catch (e) { failed.push(`#${i.id}: ${errorText(e)}`); }
    }
    setBusy(false);
    setPicked(new Set());
    if (failed.length) toast.error(`${chosen.length - failed.length} of ${chosen.length} handed to ${who}. ${failed.join("; ")}`);
    else toast.success(`${chosen.length} handed to ${who}`);
    onDone();
  };
  return (
    <section className="card soc-sup-card">
      <div className="row soc-sup-head">
        <h3>Open incidents <span className="muted small">({rows.length})</span></h3>
        <select aria-label="Customer" value={f.org ?? ""} onChange={(e) => setF({ ...f, org: e.target.value || null })}>
          <option value="">All customers</option>
          {customers.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
        </select>
        <select aria-label="Priority" value={f.priority ?? ""} onChange={(e) => setF({ ...f, priority: (e.target.value || null) as Priority | null })}>
          <option value="">Any priority</option>
          {PRIORITIES.map((p) => <option key={p} value={p}>{priorityLabel(p)}</option>)}
        </select>
        <select aria-label="State" value={f.state ?? ""} onChange={(e) => setF({ ...f, state: (e.target.value || null) as OpenFilters["state"] })}>
          <option value="">Any state</option>
          {(["new", "claimed", "pending_verify"] as const).map((s) => <option key={s} value={s}>{STATE_LABEL[s]}</option>)}
        </select>
        <select aria-label="Escalation" value={f.escalation ?? ""} onChange={(e) => setF({ ...f, escalation: e.target.value === "" ? null : Number(e.target.value) })}>
          <option value="">Any escalation</option>
          <option value="1">Escalated (L1+)</option>
          <option value="2">Supervisors paged (L2+)</option>
          <option value="3">Customer contact (L3)</option>
        </select>
        {chosen.length > 0 && (
          <select aria-label={`Hand ${chosen.length} selected off to`} disabled={busy} value="" onChange={(e) => e.target.value && bulk(e.target.value)}>
            <option value="">{busy ? "Handing off…" : `Hand ${chosen.length} off to…`}</option>
            {roster.filter((p) => p.status !== "offline" && p.on_shift !== false).map((p) => <option key={p.user_id} value={p.user_id}>{p.email} ({STATUS_LABEL[p.status] ?? p.status})</option>)}
          </select>
        )}
      </div>
      {rows.length === 0 ? <p className="muted small">Nothing open{f !== NO_OPEN_FILTERS ? " with these filters" : ""}.</p> : (
        <table className="hub-table soc-open">
          <thead><tr>
            <th><input type="checkbox" aria-label="Select every incident that can be handed off" checked={handable.length > 0 && chosen.length === handable.length}
              onChange={(e) => setPicked(e.target.checked ? new Set(handable.map((i) => i.id)) : new Set())} /></th>
            <th>#</th><th>Priority</th><th>Where</th><th>What</th><th>State</th><th>Clock</th><th>Operator</th>
          </tr></thead>
          <tbody>
            {rows.map((i) => (
              <tr key={i.id} className={i.escalation_level >= 2 ? "soc-row-escalated" : ""}>
                <td><input type="checkbox" aria-label={`Select #${i.id}`} disabled={!(i.state === "new" || i.state === "claimed")} checked={picked.has(i.id)} onChange={(e) => toggle(i.id, e.target.checked)} /></td>
                <td><button className="soc-linklike" onClick={() => onOpen(i.id)}>#{i.id}</button></td>
                <td><span className={priorityClass(i.priority)}>{priorityLabel(i.priority)}</span>{i.lane === "quiet" && <span className="muted small"> quiet</span>}</td>
                <td>{i.org_name} › {i.location_name}</td>
                <td>{incidentTitle(i)}{i.escalation_level > 0 && <span className={`chip small ${i.escalation_level >= 2 ? "warn" : ""}`}> L{i.escalation_level}</span>}</td>
                <td>{STATE_LABEL[i.state] ?? i.state}</td>
                <td><SlaPill incident={i} now={now} /> <span className="muted small">{fmtDur(now - i.opened_at)}</span></td>
                <td>{i.claimed_by_email ?? <span className="soc-unclaimed">unclaimed</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

// ---------------------------------------------------------------- arming overrides

type Override = { mode?: string; until?: number; by?: string | null; reason?: string; at?: number };

function ArmingPanel({ sites, reload, now }: { sites: SocSite[] | null; reload: () => void; now: number }) {
  const [dur, setDur] = useState<OverrideDuration>("next");
  const [busy, setBusy] = useState<string | null>(null);
  const when = (s: SocSite, ts: number) => (s.timezone ? fmtInZone(ts, s.timezone, now) : new Date(ts * 1000).toLocaleString());
  // "until the next scheduled change": the hub's own next_change (its schedule, ignoring the override), capped at 24 h
  const untilFor = (s: SocSite) => {
    const cap = Math.floor(now) + 24 * 3600;
    if (dur !== "next") return Math.min(cap, Math.floor(now) + Number(dur) * 3600);
    return Math.min(cap, s.next_change?.at && s.next_change.at > now ? Math.floor(s.next_change.at) : cap);
  };
  const act = async (s: SocSite, mode: "arm" | "disarm") => {
    const reason = await promptDialog(mode === "arm" ? `Arm ${s.name} now` : `Disarm ${s.name} now`,
      { message: `${s.org_name ?? ""} › ${s.name}, ${OVERRIDE_DURATIONS.find(([v]) => v === dur)?.[1]}.`, label: "Reason (kept in the audit log)", confirmLabel: mode === "arm" ? "Arm" : "Disarm" });
    if (reason == null) return;
    if (!reason.trim()) { toast.error("A reason is required"); return; }
    setBusy(s.id);
    try { await api.arm(s.id, { mode, until: untilFor(s), reason: reason.trim() }); toast.success(`${s.name} ${mode === "arm" ? "armed" : "disarmed"}`); reload(); }
    catch (e) { toast.error(e); } finally { setBusy(null); }
  };
  const clear = async (s: SocSite) => {
    setBusy(s.id);
    try { await api.clearArm(s.id); toast.success(`${s.name} is back on its schedule`); reload(); } catch (e) { toast.error(e); } finally { setBusy(null); }
  };
  return (
    <section className="card soc-sup-card">
      <div className="row soc-sup-head">
        <h3>Arming</h3>
        <label className="muted small">Arm / disarm now
          <select value={dur} onChange={(e) => setDur(e.target.value as OverrideDuration)} aria-label="For how long">
            {OVERRIDE_DURATIONS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
          </select>
        </label>
      </div>
      {sites == null ? <p className="muted small">Loading Sites…</p> : sites.length === 0 ? <p className="muted small">No Site is monitored yet. Customers (or a supervisor) opt a Site in under Site → Settings → Monitoring.</p> : (
        <table className="hub-table soc-arming">
          <thead><tr><th>Site</th><th>Now</th><th>Next change</th><th>Override</th><th>Open</th><th>Servers</th><th /></tr></thead>
          <tbody>
            {sites.map((s) => {
              const o = (s.override && typeof s.override === "object" ? s.override : null) as Override | null;
              return (
                <tr key={s.id}>
                  <td>{s.org_name} › {s.name}{!s.timezone && <span className="muted small"> (no time zone)</span>}</td>
                  <td><span className={`dot ${s.armed ? "ok" : "soc-dot-offline"}`} aria-hidden /> {s.armed ? "Armed" : "Disarmed"} <span className="muted small">{REASON_LABEL[s.reason] ?? s.reason}</span></td>
                  <td>{s.next_change ? `${s.next_change.armed ? "arms" : "disarms"} ${when(s, s.next_change.at)}` : <span className="muted">—</span>}</td>
                  <td>{o ? <span className="chip small warn" title={o.reason ? `“${o.reason}”` : undefined}>{o.mode === "arm" ? "Armed" : "Disarmed"} by {o.by ?? "someone"}{o.until ? ` until ${when(s, o.until)}` : ""}</span> : <span className="muted">—</span>}</td>
                  <td>{s.open_incidents}{s.ringing ? <span className="soc-unclaimed"> · {s.ringing} ringing</span> : null}</td>
                  <td className={s.servers_online < s.servers_total ? "soc-unclaimed" : ""}>{s.servers_online}/{s.servers_total}</td>
                  <td className="soc-board-actions">
                    <button className="small" disabled={busy === s.id || !s.timezone} onClick={() => act(s, s.armed ? "disarm" : "arm")}>{s.armed ? "Disarm now" : "Arm now"}</button>
                    {o && <button className="ghost small" disabled={busy === s.id} onClick={() => clear(s)}>Clear override</button>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </section>
  );
}

// ---------------------------------------------------------------- today's resolved, four-eyes

function ResolvedList({ rows, since, label, emailOf, actions, onOpen, compact }: {
  rows: ResolvedRow[]; since: number; label: (c: string | null) => string; emailOf: (id: string | null, fallback?: string | null) => string;
  actions: Actions; onOpen: (id: number) => void; compact: boolean;
}) {
  const verify = (i: Incident) => actions.run("verify", () => socApi.verify(i.id), `#${i.id} verified`);
  const reject = async (i: Incident) => {
    const note = await promptDialog(`Send #${i.id} back to ${emailOf(i.resolved_by, i.claimed_by_email)}?`, { message: "The resolution is undone and the incident goes back to its operator with your note.", label: "What needs another look", confirmLabel: "Send back" });
    if (note == null) return;
    if (!note.trim()) { toast.error("Say what needs another look"); return; }
    actions.run("reject", () => socSupervisorApi.reject(i.id, note.trim()), `#${i.id} sent back`);
  };
  const waiting = rows.filter((r) => r.incident.state === "pending_verify").length;
  return (
    <section className="card soc-sup-card">
      <h3>Resolved this shift <span className="muted small">(since {new Date(since * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}: {rows.length}{waiting ? `, ${waiting} to verify` : ""})</span></h3>
      {rows.length === 0 ? <p className="muted small">Nothing resolved yet this shift.</p> : (
        <table className="hub-table soc-resolved">
          <thead><tr><th>#</th>{!compact && <th>Where</th>}<th>Disposition</th><th>Operator</th>{!compact && <th>To claim</th>}<th>To resolve</th><th /></tr></thead>
          <tbody>
            {rows.map(({ incident: i, claimS, resolveS, canVerify, verifyBlocked }) => (
              <tr key={i.id} className={i.state === "pending_verify" ? "soc-row-verify" : ""}>
                <td><button className="soc-linklike" onClick={() => onOpen(i.id)}>#{i.id}</button></td>
                {!compact && <td>{i.org_name} › {i.location_name}</td>}
                <td>{label(i.disposition)}{i.disposition_notes ? <span className="muted small" title={i.disposition_notes}> · {i.disposition_notes.length > 40 ? `${i.disposition_notes.slice(0, 40)}…` : i.disposition_notes}</span> : null}</td>
                <td>{emailOf(i.resolved_by, i.claimed_by_email)}</td>
                {!compact && <td>{fmtDur(claimS)}</td>}
                <td>{fmtDur(resolveS)}</td>
                <td className="soc-board-actions">
                  {i.state === "pending_verify" ? (<>
                    {/* the hub refuses self-verification too (its 403 text shows as a toast if this list is stale) */}
                    <button className="small" disabled={!!actions.busy || !canVerify} title={verifyBlocked ?? "Four-eyes: confirm this resolution"} onClick={() => verify(i)}>Verify</button>
                    <button className="ghost small" disabled={!!actions.busy} onClick={() => reject(i)}>Reject…</button>
                    {verifyBlocked && <span className="muted small">you resolved it</span>}
                  </>) : <span className="muted small">{i.four_eyes_by ? "verified" : "closed"}</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

// ---------------------------------------------------------------- SLA policy

/**
 * Time to claim and to resolve per priority, and its lane. New incidents use the policy; open ones keep the clocks
 * they started with (the hub's rule), so a change here never moves a deadline someone is working against.
 */
/** the editor's live preview of a seconds field ("—" while blank or not a number) */
const secs = (v: string) => { const n = parseSeconds(v); return typeof n === "number" ? n : null; };

function SlaEditor() {
  const [cur, setCur] = useState<{ sla: SlaPolicy; defaults?: SlaPolicy } | null>(null);
  const [draft, setDraft] = useState<SlaDraft | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { socSupervisorApi.slaFull().then((r) => { setCur(r); setDraft(slaDraft(r.sla)); }).catch(() => {}); }, []);
  if (!cur || !draft) return null;
  const apply = (r: { sla: SlaPolicy; defaults?: SlaPolicy }) => { setCur(r); setDraft(slaDraft(r.sla)); };
  const save = async () => {
    const r = slaPatch(draft, cur.sla, parseSeconds);
    if ("error" in r) { toast.error(r.error); return; }
    if (!Object.keys(r.patch).length) { toast.info("Nothing changed"); return; }
    setBusy(true);
    try { apply(await socSupervisorApi.putSla(r.patch)); toast.success("SLA saved: new incidents use it"); } catch (e) { toast.error(e); } finally { setBusy(false); }
  };
  const reset = async (p: Priority) => {
    setBusy(true);
    try { apply(await socSupervisorApi.putSla({ [p]: null })); toast.success(`${priorityLabel(p)} back to the defaults`); } catch (e) { toast.error(e); } finally { setBusy(false); }
  };
  const set = (p: Priority, k: keyof SlaDraft[Priority], v: string) => setDraft({ ...draft, [p]: { ...draft[p], [k]: v } });
  const def = (p: Priority) => cur.defaults?.[p];
  return (
    <details className="card soc-sup-card soc-sla-editor">
      <summary><h3>SLA policy</h3></summary>
      <p className="muted small">Seconds. Blank = no clock. Open incidents keep the deadlines they started with.</p>
      <table className="hub-table">
        <thead><tr><th>Priority</th><th>Time to claim (s)</th><th>Time to resolve (s)</th><th>Lane</th><th>Defaults</th><th /></tr></thead>
        <tbody>
          {PRIORITIES.map((p) => (
            <tr key={p}>
              <td><span className={priorityClass(p)}>{priorityLabel(p)}</span></td>
              <td><input inputMode="numeric" size={6} aria-label={`${p} time to claim, seconds`} value={draft[p].claim} onChange={(e) => set(p, "claim", e.target.value)} /> <span className="muted small">{fmtDur(secs(draft[p].claim))}</span></td>
              <td><input inputMode="numeric" size={6} aria-label={`${p} time to resolve, seconds`} value={draft[p].resolve} onChange={(e) => set(p, "resolve", e.target.value)} /> <span className="muted small">{fmtDur(secs(draft[p].resolve))}</span></td>
              <td><select aria-label={`${p} lane`} value={draft[p].lane} onChange={(e) => set(p, "lane", e.target.value)}><option value="ring">Ringing</option><option value="quiet">Quiet</option></select></td>
              <td className="muted small">{def(p) ? `${fmtDur(def(p)!.claim_s)} / ${fmtDur(def(p)!.resolve_s)} · ${def(p)!.lane ?? "ring"}` : "—"}</td>
              <td><button className="ghost small" disabled={busy} onClick={() => reset(p)}>Defaults</button></td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="row" style={{ marginTop: 8 }}>
        <button disabled={busy} onClick={save}>{busy ? "Saving…" : "Save SLA"}</button>
        <button className="ghost" disabled={busy} onClick={() => setDraft(slaDraft(cur.sla))}>Undo changes</button>
      </div>
    </details>
  );
}
