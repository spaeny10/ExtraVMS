/**
 * The centre of the workstation: one incident. Header (customer › Site, priority, SLA timer, state, escalation,
 * claimer), the claim bar, the triggering clip and snapshot with a strip of every event in the incident, the event's
 * synopsis and why it matters, live video of the Site with the incident's cameras first, the Site's Timeline
 * deep-linked to the selected event, deterrence (relay), and the log.
 *
 * Everything per camera goes to that camera's server through the hub tunnel (siteApi(server)), as on Site pages.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import type { Camera as ServerCam, NvrEvent } from "@site/api";
import { ActionCard, type ActionPlanCore, type ActionResult } from "@site/ActionCard";
import { EventDetail } from "@site/EventDetail";
import { placeholder } from "@site/Events";
import { NavContext, type TimelineTarget } from "@site/nav";
import { camKey } from "@site/playback";
import { confirmDialog } from "@site/ui";
import type { Me } from "../api";
import { isSocSupervisor } from "../access";
import { siteApi } from "../hubSource";
import { incidentHref, siteHref } from "../nav";
import { useSite } from "../SitePage";
import { SiteLive } from "../SiteLive";
import { SiteTimeline } from "../SiteTimeline";
import { siteTimelineHref } from "../timelineLink";
import { STATE_LABEL, age, eventNum, incidentTitle } from "./format";
import { IncidentLog } from "./IncidentLog";
import { priorityClass, priorityLabel, slaFor } from "./sla";
import { socApi } from "./socApi";
import { claimedByMe, incidentOrg, useCommand } from "./useIncident";
import { useSoc } from "./useSocStream";
import type { Incident, IncidentDetail, IncidentEvent, Presence } from "./types";

/** A command from the page's keyboard handler (nonce makes the same key twice count twice). */
export type ViewCmd = { type: string; nonce: number; index?: number; code?: string };
type Actions = { busy: string | null; run: <T>(name: string, fn: () => Promise<T>, ok?: string) => Promise<T | null> };

const TIMELINE_OPEN_MIN_PX = 1400;

export function IncidentView({ me, detail, incident, now, actions, cmd, popout = false }: {
  me: Me; detail: IncidentDetail | null; incident: Incident; now: number; actions: Actions; cmd: ViewCmd | null; popout?: boolean;
}) {
  const events = useMemo(() => [...(detail?.events ?? incident.events ?? [])].sort((a, b) => a.ts - b.ts), [detail, incident.events]);
  // the event shown: the newest by default (the current state of things), any thumbnail to switch
  const [selKey, setSelKey] = useState<string | null>(null);
  useEffect(() => setSelKey(null), [incident.id]);
  const evKey = (e: IncidentEvent) => `${e.server_id}:${e.event_id}`;
  const sel = events.find((e) => evKey(e) === selKey) ?? events[events.length - 1] ?? null;

  const [timelineOpen, setTimelineOpen] = useState(() => typeof innerWidth === "number" && innerWidth >= TIMELINE_OPEN_MIN_PX);
  const [timelineQuery, setTimelineQuery] = useState<string>("");
  useEffect(() => {
    if (!sel) return;
    setTimelineQuery(`?${new URLSearchParams({ server: sel.server_id, cam: sel.camera_id, event: String(sel.event_id) })}`);
  }, [sel?.server_id, sel?.event_id]);  // eslint-disable-line react-hooks/exhaustive-deps
  const [drawer, setDrawer] = useState<IncidentEvent | null>(null);
  const liveRef = useRef<HTMLElement>(null);
  const timelineRef = useRef<HTMLDetailsElement>(null);

  /** "Open in Timeline" from the details drawer: focus the inline Timeline instead of leaving the console. */
  const focusTimeline = (server: string, e: Pick<TimelineTarget, "camera_id" | "id">) => {
    setTimelineQuery(`?${new URLSearchParams({ server, cam: e.camera_id, event: String(e.id), n: String(Date.now()) })}`);
    setTimelineOpen(true);
    requestAnimationFrame(() => timelineRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }));
  };

  useCommand(cmd, (c) => {
    if (c.type === "goLive") liveRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    if (c.type === "goTimeline") { setTimelineOpen(true); requestAnimationFrame(() => timelineRef.current?.scrollIntoView({ behavior: "smooth", block: "start" })); }
    if (c.type === "goDetails" && sel) setDrawer(sel);
  });

  const { site } = useSite(incident.location_id);
  const org = incidentOrg(me, incident);
  // the incident's cameras first: from its events once the detail is in, from the queue row's camera list until then
  const focusKeys = useMemo(() => [...new Set(events.length ? events.map((e) => camKey(e.server_id, e.camera_id))
    : (incident.cameras ?? []).map((c) => camKey(c.server_id, c.camera_id)))], [events, incident.cameras]);
  const [onlyIncidentCams, setOnlyIncidentCams] = useState(false);

  return (
    <div className="soc-incident">
      <IncidentHeader incident={incident} now={now} popout={popout} />
      <ClaimBar incident={incident} me={me} actions={actions} cmd={cmd} />
      <section className="soc-media" aria-label="Triggering event">
        {sel ? <EventMedia key={evKey(sel)} ev={sel} onDetails={() => setDrawer(sel)} /> : <p className="muted">No events in this incident yet.</p>}
        {events.length > 1 && (
          <div className="soc-strip" role="listbox" aria-label="Events in this incident" aria-orientation="horizontal">
            {events.map((e) => (
              <button key={evKey(e)} role="option" aria-selected={sel ? evKey(e) === evKey(sel) : false} className={`soc-thumb ${sel && evKey(e) === evKey(sel) ? "active" : ""}`}
                onClick={() => setSelKey(evKey(e))} title={`${e.camera_name} · ${new Date(e.ts * 1000).toLocaleTimeString()}`}>
                <img src={siteApi(e.server_id).media({ id: eventNum(e) }, "snapshot.jpg")} alt="" loading="lazy" onError={(x) => { (x.target as HTMLImageElement).style.visibility = "hidden"; }} />
                <span className="small">{e.camera_name} · {age(e.ts, now)}</span>
                <span className={priorityClass(e.priority)} aria-label={`${priorityLabel(e.priority)} priority`}>{priorityLabel(e.priority)}</span>
              </button>
            ))}
          </div>
        )}
      </section>

      <section ref={liveRef} className="soc-live" aria-label="Live video">
        <div className="row soc-section-head">
          <h3>Live</h3>
          <label className="small" title="Show only the cameras that saw this incident, or put them first among the Site's cameras">
            <input type="checkbox" checked={onlyIncidentCams} onChange={(e) => setOnlyIncidentCams(e.target.checked)} /> Only incident cameras
          </label>
          <span className="spacer" />
          <a className="small" href={siteHref(incident.location_id, "live")} target="_blank" rel="noreferrer">Open the Site</a>
        </div>
        {site ? <SiteLive org={org} site={site} focus={{ keys: focusKeys, mode: onlyIncidentCams ? "only" : "first" }} persist={false} compact hideActivity />
          : <p className="muted small">Loading the Site…</p>}
      </section>

      <details ref={timelineRef} className="soc-timeline" open={timelineOpen} onToggle={(e) => setTimelineOpen((e.target as HTMLDetailsElement).open)}>
        <summary><h3>Timeline</h3>{sel && <a className="small" href={siteTimelineHref(incident.location_id, sel.server_id, sel.camera_id, eventNum(sel))} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()}>Open in the Site's Timeline</a>}</summary>
        {/* mounted only while open: the Timeline loads every lane's recordings */}
        {timelineOpen && site && <SiteTimeline org={org} site={site} query={timelineQuery} syncUrl={false} />}
      </details>

      <DeterrenceCard incident={incident} me={me} events={events} />

      <section className="card soc-log-card" aria-label="Incident log">
        <h3>Log</h3>
        <IncidentLog rows={detail?.log ?? []} contacts={detail?.contacts} />
      </section>

      {drawer && (
        <NavContext.Provider value={{ openInTimeline: (e) => focusTimeline(drawer.server_id, e) }}>
          <EventDetail id={eventNum(drawer)} site={siteApi(drawer.server_id)} variant="drawer" onClose={() => setDrawer(null)}
            cameraName={(id) => events.find((x) => x.server_id === drawer.server_id && x.camera_id === id)?.camera_name ?? id} />
        </NavContext.Provider>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- header + claim bar

export function SlaPill({ incident, now }: { incident: Incident; now: number }) {
  const { sla } = useSoc();
  const v = slaFor(incident, now, sla);
  if (!v) return null;
  return <span className={`soc-sla ${v.tone}`} role="timer" aria-label={v.label} title={v.label}>{v.tone === "breach" ? "⚠ " : ""}{v.text}</span>;
}

export function IncidentHeader({ incident: i, now, popout }: { incident: Incident; now: number; popout?: boolean }) {
  return (
    <header className="soc-incident-head">
      <div className="soc-incident-where">
        <span className="muted small">{i.org_name} › <a href={siteHref(i.location_id, "live")} target="_blank" rel="noreferrer">{i.location_name}</a></span>
        <h2 tabIndex={-1} id="soc-incident-title">{incidentTitle(i)} <span className="muted small">#{i.id}</span></h2>
      </div>
      <div className="row soc-incident-chips">
        <span className={priorityClass(i.priority)} aria-label={`${priorityLabel(i.priority)} priority`}>{priorityLabel(i.priority)}</span>
        <SlaPill incident={i} now={now} />
        <span className={`chip small soc-state-${i.state}`}>{STATE_LABEL[i.state] ?? i.state}</span>
        {i.lane === "quiet" && <span className="chip small" title="Low priority: swept in bulk, no alarm">Quiet lane</span>}
        {i.escalation_level > 0 && <span className={`chip small ${i.escalation_level >= 2 ? "warn" : ""}`} title={i.escalation_level >= 2 ? "Supervisors have been paged" : "Operators have been paged"}>Escalated L{i.escalation_level}</span>}
        {i.claimed_by_email && <span className="chip small" title={i.claimed_at ? `Claimed ${age(i.claimed_at, now)} ago` : ""}>👤 {i.claimed_by_email}</span>}
        <span className="muted small" title={new Date(i.opened_at * 1000).toLocaleString()}>opened {age(i.opened_at, now)} ago · {i.event_count} event{i.event_count === 1 ? "" : "s"}</span>
        {!popout && <a className="small" href={incidentHref(i.id)} target="_blank" rel="noreferrer" aria-keyshortcuts="P" title="Pop out (p)">Pop out ↗</a>}
      </div>
    </header>
  );
}

/**
 * Claim / release / hand off / take over. One request at a time (the bar disables while one is out). Hand off picks
 * from the roster (available or engaged SOC staff other than the holder). Take over is a supervisor's, confirmed.
 */
export function ClaimBar({ incident: i, me, actions, cmd, compact = false }: { incident: Incident; me: Me; actions: Actions; cmd?: ViewCmd | null; compact?: boolean }) {
  const { queue } = useSoc();
  const mine = claimedByMe(i, me);
  const sup = isSocSupervisor(me);
  const [picking, setPicking] = useState(false);
  useCommand(cmd, (c) => { if (c.type === "handoff" && (mine || sup) && i.state === "claimed") setPicking(true); });
  useEffect(() => setPicking(false), [i.id]);
  const b = actions.busy;
  const claim = () => actions.run("claim", () => socApi.claim(i.id));
  const release = () => actions.run("release", () => socApi.release(i.id), "Released to the queue");
  const takeover = async () => {
    if (!(await confirmDialog(`Take over incident #${i.id} from ${i.claimed_by_email ?? "its operator"}?`, { message: "They lose the claim and are told so in the log.", confirmLabel: "Take over", danger: true }))) return;
    actions.run("takeover", () => socApi.takeover(i.id), "You have this incident now");
  };
  const handoff = (p: Presence) => { setPicking(false); actions.run("handoff", () => socApi.handoff(i.id, p.user_id), `Handed to ${p.email}`); };
  const roster = queue.presence.filter((p) => p.user_id !== i.claimed_by && p.status !== "offline");
  if (i.state === "closed" || i.state === "pending_verify") {
    return (
      <div className="row soc-claimbar">
        <span className="muted small">{i.state === "closed" ? "Closed" : "Resolved, awaiting supervisor verification"}{i.disposition ? ` · ${i.disposition.replace(/_/g, " ")}` : ""}</span>
        {i.state === "pending_verify" && sup && i.resolved_by !== me.user.id && (
          <button className="small" disabled={!!b} onClick={() => actions.run("verify", () => socApi.verify(i.id), "Verified")}>{b === "verify" ? "Verifying…" : "Verify"}</button>
        )}
      </div>
    );
  }
  return (
    <div className={`row soc-claimbar ${compact ? "compact" : ""}`}>
      {i.state === "new" && <button className="soc-claim" disabled={!!b} aria-keyshortcuts="C" onClick={claim}>{b === "claim" ? "Claiming…" : "Claim"}</button>}
      {mine && <button className="ghost small" disabled={!!b} aria-keyshortcuts="L" onClick={release}>{b === "release" ? "Releasing…" : "Release"}</button>}
      {(mine || (sup && i.state === "claimed")) && <button className="ghost small" disabled={!!b} aria-keyshortcuts="H" aria-expanded={picking} onClick={() => setPicking((x) => !x)}>Hand off…</button>}
      {sup && i.state === "claimed" && !mine && <button className="ghost small" disabled={!!b} onClick={takeover}>{b === "takeover" ? "Taking over…" : "Take over"}</button>}
      {i.state === "claimed" && !mine && <span className="muted small">{i.claimed_by_email ?? "Another operator"} has this incident.</span>}
      {mine && <span className="muted small">You have this incident.</span>}
      {picking && (
        <div className="soc-handoff" role="group" aria-label="Hand off to">
          {roster.length === 0 ? <span className="muted small">Nobody else is on shift.</span> : roster.map((p) => (
            <button key={p.user_id} className="ghost small" disabled={!!b} onClick={() => handoff(p)} title={p.status === "engaged" ? `Engaged${p.incident_id ? ` on #${p.incident_id}` : ""}` : p.status}>
              {p.email} <span className="muted">({p.status === "break" ? "on break" : p.status})</span>
            </button>
          ))}
          <button className="ghost small" onClick={() => setPicking(false)}>Cancel</button>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- media + synopsis

/**
 * The event's clip (or snapshot until the clip is cut) from its server. Media is written a few seconds after an event
 * opens, so while either is missing the event is re-read every 3 s (at most 3 minutes; then the operator has Live).
 */
export function useServerEvent(server: string, id: number) {
  const [e, setE] = useState<NvrEvent | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let alive = true;
    const start = Date.now();
    let t: ReturnType<typeof setTimeout> | null = null;
    const pull = () => siteApi(server).event(id).then((x) => {
      if (!alive) return;
      setE(x); setFailed(false);
      const waiting = (!x.clip || !x.snapshot || x.status === "open" || x.status === "pending") && x.status !== "rejected";
      if (waiting && Date.now() - start < 180000) t = setTimeout(pull, 3000);
    }).catch(() => { if (alive) { setFailed(true); if (Date.now() - start < 180000) t = setTimeout(pull, 3000); } });
    pull();
    return () => { alive = false; if (t) clearTimeout(t); };
  }, [server, id]);
  return { e, failed };
}

export function EventMedia({ ev, onDetails }: { ev: IncidentEvent; onDetails?: () => void }) {
  const { e, failed } = useServerEvent(ev.server_id, eventNum(ev));
  const s = siteApi(ev.server_id);
  return (
    <div className="soc-media-grid">
      <div className="soc-clip">
        {e?.clip ? <video src={s.media(e, "clip.mp4")} poster={e.snapshot ? s.media(e, "snapshot.jpg") : undefined} controls autoPlay muted loop playsInline />
          : e?.snapshot ? <img src={s.media(e, "snapshot.jpg")} alt={`Snapshot from ${ev.camera_name}`} />
          : <div className="soc-clip-wait muted small">{failed ? `${ev.server_name} isn't answering; retrying…` : "Waiting for the clip…"}</div>}
        <div className="muted small">{ev.camera_name}{ev.server_name ? ` · ${ev.server_name}` : ""} · {new Date(ev.ts * 1000).toLocaleTimeString()}{e && !e.clip ? " · clip still being cut" : ""}</div>
      </div>
      <IncidentSynopsis ev={ev} e={e} onDetails={onDetails} />
    </div>
  );
}

/** What the site's AI said and why this event matters: synopsis, why it is unusual, the rule it broke, watched, areas. */
export function IncidentSynopsis({ ev, e, onDetails }: { ev: IncidentEvent; e: NvrEvent | null; onDetails?: () => void }) {
  const d = (ev.detail ?? {}) as Record<string, unknown>;
  const synopsis = e?.synopsis ?? (typeof d.synopsis === "string" ? d.synopsis : null);
  const reasons = e?.anomaly_json?.reasons ?? (Array.isArray(d.reasons) ? (d.reasons as string[]) : []);
  const rule = e?.policy?.text ?? (typeof d.rule === "string" ? d.rule : null);
  const watched = e?.watched ?? (typeof d.watched === "string" ? d.watched : null);
  const areas = e?.areas?.map((a) => a.name) ?? [];
  return (
    <div className="soc-synopsis">
      <p className={synopsis ? "" : "muted"}>{synopsis ?? (e ? placeholder(e) : "Loading the event…")}</p>
      <dl>
        {reasons.length > 0 && <><dt>Why unusual</dt><dd><ul>{reasons.map((r) => <li key={r}>{r}</li>)}</ul></dd></>}
        {rule && <><dt>Rule</dt><dd>{rule}</dd></>}
        {watched && <><dt>Watched</dt><dd>👁 {watched}</dd></>}
        {areas.length > 0 && <><dt>Areas</dt><dd>📍 {areas.join(" → ")}</dd></>}
      </dl>
      {onDetails && <button className="ghost small" onClick={onDetails} aria-keyshortcuts="G D">Full details</button>}
    </div>
  );
}

// ---------------------------------------------------------------- deterrence

/**
 * Relays (sirens, floodlights, gate strobes wired to a camera's alarm output) at the incident's servers. The hub
 * switches them through the tunnel and logs it on the incident; the card says exactly what happens before Confirm.
 * Talk-down through a camera speaker is a later stage (site-side audio out), shown disabled so operators know.
 */
function DeterrenceCard({ incident: i, me, events }: { incident: Incident; me: Me; events: IncidentEvent[] }) {
  const servers = useMemo(() => [...new Set([...events.map((e) => e.server_id), ...(i.servers ?? []).map((s) => s.id)])], [events, i.servers]);
  const [cams, setCams] = useState<{ server: string; serverName: string; cam: ServerCam }[]>([]);
  const serverKey = servers.join(",");
  useEffect(() => {
    let alive = true;
    Promise.all(servers.map((s) => siteApi(s).cameras().then((list) => list.filter((c) => c.status?.ptz?.relay).map((cam) => ({ server: s, serverName: events.find((e) => e.server_id === s)?.server_name ?? i.servers?.find((x) => x.id === s)?.name ?? s, cam }))).catch(() => [])))
      .then((r) => { if (alive) setCams(r.flat()); });
    return () => { alive = false; };
  }, [serverKey]);  // eslint-disable-line react-hooks/exhaustive-deps
  const [plan, setPlan] = useState<{ server: string; cam: ServerCam; on: boolean } | null>(null);
  const can = claimedByMe(i, me) || (isSocSupervisor(me) && i.state === "claimed");
  const inIncident = (server: string, cam: string) => events.some((e) => e.server_id === server && e.camera_id === cam);
  const sorted = [...cams].sort((a, b) => Number(inIncident(b.server, b.cam.id)) - Number(inIncident(a.server, a.cam.id)));
  if (!sorted.length) {
    return (
      <section className="card soc-deter" aria-label="Deterrence">
        <h3>Deterrence</h3>
        <p className="muted small">No relay outputs at this Site's cameras. <button className="ghost small" disabled title="Talk-down through a camera speaker arrives with site-side audio out">🔊 Talk-down (coming)</button></p>
      </section>
    );
  }
  const core = (p: NonNullable<typeof plan>): ActionPlanCore => {
    const label = p.cam.status?.ptz?.relay?.label || "relay";
    return {
      id: `relay-${p.server}-${p.cam.id}`, action: "relay", allowed: true,
      card: {
        title: `${p.on ? "Switch on" : "Switch off"} ${label} at ${p.cam.name}`,
        moves: [`${p.cam.name}'s alarm output (${label}) switches ${p.on ? "on" : "off"} at ${i.location_name}.`, "The switch is written to this incident's log."],
        stays: ["Recording and detection carry on unchanged."],
        warnings: p.on && p.cam.status?.ptz?.relay?.mode === "bistable" ? ["This output stays on until someone switches it off."] : [],
        blockers: can ? [] : ["Claim the incident first."], needs: [], can_execute: can,
      },
    };
  };
  const execute = async (): Promise<ActionResult> => {
    if (!plan) throw new Error("No relay chosen");
    await socApi.relay(i.id, plan.server, plan.cam.id, plan.on);
    const r: ActionResult = { ok: true, lines: [`${plan.cam.name}: relay ${plan.on ? "on" : "off"}`], summary: "Relay switched" };
    // refresh the state shown on the buttons
    siteApi(plan.server).cameras().then((list) => setCams((cs) => cs.map((c) => (c.server === plan.server ? { ...c, cam: list.find((x) => x.id === c.cam.id) ?? c.cam } : c)))).catch(() => {});
    return r;
  };
  return (
    <section className="card soc-deter" aria-label="Deterrence">
      <h3>Deterrence</h3>
      <div className="row">
        {sorted.map((c) => {
          const relay = c.cam.status!.ptz!.relay!;
          const isOn = relay.state === true;
          return (
            <button key={`${c.server}/${c.cam.id}`} className={`small ${isOn ? "" : "ghost"}`} disabled={!can} title={can ? "" : "Claim the incident first"}
              onClick={() => setPlan({ server: c.server, cam: c.cam, on: !isOn })}>
              {isOn ? "⏻ " : "⚡ "}{relay.label || "Relay"} · {c.cam.name}{inIncident(c.server, c.cam.id) ? "" : ` (${c.serverName})`}{isOn ? " · on" : ""}
            </button>
          );
        })}
        <button className="ghost small" disabled title="Talk-down through a camera speaker arrives with site-side audio out">🔊 Talk-down (coming)</button>
      </div>
      {plan && <ActionCard key={`${plan.server}/${plan.cam.id}/${plan.on}`} plan={core(plan)} kind="deterrence" onExecute={execute} onClose={() => setPlan(null)} />}
      {!can && <p className="muted small">Claim the incident to use deterrence.</p>}
    </section>
  );
}
