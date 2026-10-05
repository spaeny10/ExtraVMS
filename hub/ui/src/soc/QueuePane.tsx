/**
 * The workstation's left column: counts, the filter box (/) and chips, the ringing lane in work order (queue.ts),
 * and the quiet lane collapsed underneath with its sweep grid.
 *
 * The ringing lane is a listbox: j/k move the highlight (aria-activedescendant), Enter or a click opens the incident
 * (aria-selected). Moving the highlight doesn't open anything, so stepping past rows doesn't start live video for each.
 */
import { forwardRef, useEffect, useMemo, useState } from "react";
import type { NvrEvent } from "@site/api";
import { EventCard } from "@site/Events";
import type { Org } from "../api";
import { siteApi } from "../hubSource";
import { age, cameraNames, eventNum, incidentTitle } from "./format";
import { type QueueFilters, NO_FILTERS } from "./queue";
import { priorityClass, priorityLabel } from "./sla";
import { SlaPill } from "./IncidentView";
import { socApi } from "./socApi";
import type { Incident, IncidentEvent, Priority } from "./types";

export const rowId = (id: number) => `soc-q-${id}`;

type Props = {
  ringing: Incident[]; quiet: Incident[]; totalRinging: number; unclaimed: number; mine: number;
  filters: QueueFilters; setFilters: (f: QueueFilters) => void; customers: Pick<Org, "id" | "name">[];
  selected: number | null; cursor: number | null; onOpen: (id: number) => void; now: number; meId: string;
  /** arrow keys inside the listbox (j/k work anywhere on the page) */
  onStep: (dir: 1 | -1) => void;
  onSweepFalse: (i: Incident) => void; onPromote: (i: Incident) => void; onSweepSite: (locationId: string, name: string) => void; busy: string | null;
};

export const QueuePane = forwardRef<HTMLInputElement, Props>(function QueuePane(p, filterRef) {
  const f = p.filters;
  const set = (x: Partial<QueueFilters>) => p.setFilters({ ...f, ...x });
  const filtered = f.org || f.priority || f.mine || f.unclaimed || f.text;
  const [quietOpen, setQuietOpen] = useState(false);
  return (
    <div className="soc-queue">
      <div className="soc-counts" aria-live="off">
        <span className={p.unclaimed ? "soc-count hot" : "soc-count"}><strong>{p.unclaimed}</strong> unclaimed</span>
        <span className="soc-count"><strong>{p.totalRinging}</strong> ringing</span>
        <span className="soc-count"><strong>{p.mine}</strong> mine</span>
        <span className="soc-count"><strong>{p.quiet.length}</strong> quiet</span>
      </div>
      <input ref={filterRef} type="search" className="soc-filter" placeholder="Filter (/)" aria-label="Filter the queue" aria-keyshortcuts="/"
        value={f.text} onChange={(e) => set({ text: e.target.value })} onKeyDown={(e) => { if (e.key === "Escape") { (e.target as HTMLInputElement).blur(); } }} />
      <div className="row soc-chips" role="group" aria-label="Queue filters">
        {p.customers.length > 1 && (
          <select value={f.org ?? ""} onChange={(e) => set({ org: e.target.value || null })} aria-label="Customer">
            <option value="">All customers</option>
            {p.customers.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
        )}
        <select value={f.priority ?? ""} onChange={(e) => set({ priority: (e.target.value || null) as Priority | null })} aria-label="Priority">
          <option value="">Any priority</option><option value="high">High</option><option value="medium">Medium</option><option value="low">Low</option>
        </select>
        <button className={`chip small ${f.mine ? "active" : ""}`} aria-pressed={f.mine} onClick={() => set({ mine: !f.mine })}>Mine</button>
        <button className={`chip small ${f.unclaimed ? "active" : ""}`} aria-pressed={f.unclaimed} onClick={() => set({ unclaimed: !f.unclaimed })}>Unclaimed</button>
        {filtered && <button className="ghost small" onClick={() => p.setFilters(NO_FILTERS)}>Clear</button>}
      </div>

      <h3 className="soc-lane-title" id="soc-ring-title">Ringing</h3>
      {p.ringing.length === 0 ? <p className="muted small">{filtered ? "Nothing matches the filters." : "Nothing ringing. All quiet."}</p> : (
        <ul className="soc-rows" role="listbox" aria-labelledby="soc-ring-title" aria-activedescendant={p.cursor != null ? rowId(p.cursor) : undefined} tabIndex={0}
          aria-keyshortcuts="J K Enter"
          onKeyDown={(e) => { if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); p.onStep(e.key === "ArrowDown" ? 1 : -1); } }}>
          {p.ringing.map((i) => <QueueRow key={i.id} i={i} now={p.now} selected={i.id === p.selected} cursor={i.id === p.cursor} mine={i.claimed_by === p.meId} onOpen={() => p.onOpen(i.id)} />)}
        </ul>
      )}

      <details className="soc-quiet" onToggle={(e) => setQuietOpen((e.target as HTMLDetailsElement).open)}>
        <summary><span className="soc-lane-title">Quiet lane</span> <span className="muted small">{p.quiet.length} low priority</span></summary>
        {/* rendered only while open: each card fetches its incident's events for the still */}
        {quietOpen && <SweepGrid quiet={p.quiet} now={p.now} onOpen={p.onOpen} onSweepFalse={p.onSweepFalse} onPromote={p.onPromote} onSweepSite={p.onSweepSite} busy={p.busy} />}
      </details>
    </div>
  );
});

function QueueRow({ i, now, selected, cursor, mine, onOpen }: { i: Incident; now: number; selected: boolean; cursor: boolean; mine: boolean; onOpen: () => void }) {
  const cams = cameraNames(i);
  const ringing = i.state === "new" && !i.claimed_by;
  return (
    <li id={rowId(i.id)} role="option" aria-selected={selected} className={`soc-row ${selected ? "selected" : ""} ${cursor ? "cursor" : ""} ${ringing ? "ringing" : ""} soc-pri-${i.priority}`}
      onClick={onOpen}>
      <div className="soc-row-top">
        <span className={priorityClass(i.priority)} aria-label={`${priorityLabel(i.priority)} priority`}>{priorityLabel(i.priority)}</span>
        <SlaPill incident={i} now={now} />
        {i.escalation_level > 0 && <span className="chip small warn" title="Escalated">L{i.escalation_level}</span>}
        <span className="spacer" />
        <span className="muted small" title={new Date(i.opened_at * 1000).toLocaleString()}>{age(i.opened_at, now)}</span>
      </div>
      <div className="soc-row-where">{i.org_name} › {i.location_name}</div>
      <div className="small">{incidentTitle(i)}{i.event_count > 1 ? <span className="muted"> · {i.event_count} events</span> : null}</div>
      {cams.length > 0 && <div className="muted small soc-row-cams">{cams.join(", ")}</div>}
      <div className="small soc-row-claim">{i.claimed_by ? <span className={mine ? "soc-mine" : "muted"}>👤 {mine ? "You" : i.claimed_by_email ?? "claimed"}</span> : <span className="soc-unclaimed">Unclaimed</span>}</div>
    </li>
  );
}

/** An incident event as the site toolkit's EventCard wants it: enough for the still, the synopsis and the priority. */
export function cardEvent(e: IncidentEvent): NvrEvent {
  const d = (e.detail ?? {}) as Record<string, unknown>;
  // detail is what the hub copied at ingest (soc._event_detail): label, synopsis, policy, watched, start_ts
  const label = typeof d.label === "string" ? d.label : null;
  return {
    id: eventNum(e), camera_id: e.camera_id, track_id: "", camera_class: e.kind === "vehicle" ? "vehicle" : "person", camera_conf: null,
    start_ts: typeof d.start_ts === "number" ? d.start_ts : e.ts, end_ts: null, status: "verified", yolo_class: label,
    yolo_conf: null, yolo_hits: null, snapshot: "snapshot.jpg", clip: null, synopsis: typeof d.synopsis === "string" ? d.synopsis : null,
    threat: null, priority: (["high", "medium", "low", "none"].includes(e.priority) ? e.priority : null) as NvrEvent["priority"], error: null,
    watched: typeof d.watched === "string" ? d.watched : null,
    policy: d.policy && typeof d.policy === "object" ? d.policy as NvrEvent["policy"] : null,
  };
}

const SWEEP_MAX = 24;

/** One quiet incident: its newest event as an EventCard (events fetched once; queue rows don't carry them). */
function SweepCard({ i, now, onOpen, onSweepFalse, onPromote, busy }: {
  i: Incident; now: number; onOpen: (id: number) => void; onSweepFalse: (i: Incident) => void; onPromote: (i: Incident) => void; busy: string | null;
}) {
  const [events, setEvents] = useState<IncidentEvent[] | null>(i.events ?? null);
  useEffect(() => {
    if (i.events) return;
    let alive = true;
    socApi.incident(i.id).then((d) => { if (alive) setEvents(d.events); }).catch(() => { if (alive) setEvents([]); });
    return () => { alive = false; };
  }, [i.id, i.event_count, i.events]);
  const ev = [...(events ?? [])].sort((a, b) => b.ts - a.ts)[0];
  return (
    <div className="soc-sweep-card">
      {ev ? <EventCard e={cardEvent(ev)} cameraName={ev.camera_name} site={siteApi(ev.server_id)} onOpen={() => onOpen(i.id)} />
        : <button className="ghost small soc-sweep-plain" onClick={() => onOpen(i.id)}>{incidentTitle(i)} · {age(i.opened_at, now)}{events === null ? " · loading…" : ""}</button>}
      <div className="row soc-sweep-actions">
        <button className="ghost small" disabled={!!busy} onClick={() => onSweepFalse(i)}>False alarm</button>
        <button className="ghost small" disabled={!!busy} onClick={() => onPromote(i)} title="Move to the ringing lane (it rings and gets an SLA)">Promote</button>
      </div>
    </div>
  );
}

/**
 * Sweep: the quiet lane as a grid of stills, newest first, grouped by Site. Each card can be resolved as a false
 * alarm on the spot or promoted to the ringing lane; a Site's whole quiet lane can be swept in one go.
 */
function SweepGrid({ quiet, now, onOpen, onSweepFalse, onPromote, onSweepSite, busy }: {
  quiet: Incident[]; now: number; onOpen: (id: number) => void; onSweepFalse: (i: Incident) => void; onPromote: (i: Incident) => void;
  onSweepSite: (loc: string, name: string) => void; busy: string | null;
}) {
  const bySite = useMemo(() => {
    const m = new Map<string, { name: string; list: Incident[] }>();
    for (const i of quiet) {
      const g = m.get(i.location_id) ?? { name: `${i.org_name} › ${i.location_name}`, list: [] };
      g.list.push(i);
      m.set(i.location_id, g);
    }
    return [...m.entries()];
  }, [quiet]);
  if (!quiet.length) return <p className="muted small">The quiet lane is empty.</p>;
  return (
    <div className="soc-sweep">
      {bySite.map(([loc, g]) => (
        <section key={loc} className="soc-sweep-site">
          <div className="row"><strong className="small">{g.name}</strong><span className="spacer" />
            <button className="ghost small" disabled={!!busy} onClick={() => onSweepSite(loc, g.name)} title="Close every quiet incident at this Site as swept">Sweep {g.list.length}</button></div>
          <div className="soc-sweep-grid">
            {g.list.slice(0, SWEEP_MAX).map((i) => <SweepCard key={i.id} i={i} now={now} onOpen={onOpen} onSweepFalse={onSweepFalse} onPromote={onPromote} busy={busy} />)}
            {g.list.length > SWEEP_MAX && <p className="muted small">{g.list.length - SWEEP_MAX} more: sweep these, or filter by customer.</p>}
          </div>
        </section>
      ))}
    </div>
  );
}
