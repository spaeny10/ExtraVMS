/**
 * The SOC on a phone: the ringing list with a big Claim on each row, and one incident as a sheet with the clip or
 * snapshot, the synopsis, the call list as tel: links with outcome buttons, the dispositions, and Release
 * (supervisors also Take over). No live grid, timeline or keyboard: on a phone the job is to claim, call and resolve.
 */
import { useEffect, useState } from "react";
import { toast } from "@site/ui";
import type { Me } from "../api";
import { go, socHref } from "../nav";
import { age, incidentTitle } from "./format";
import { Dispatch } from "./Dispatch";
import { ClaimBar, EventMedia, SlaPill } from "./IncidentView";
import { splitLanes } from "./queue";
import { CallList, ResolvePane } from "./RightPane";
import { priorityClass, priorityLabel } from "./sla";
import { socApi } from "./socApi";
import { useIncident, useIncidentActions, useNow } from "./useIncident";
import { useSoc } from "./useSocStream";
import type { Incident } from "./types";

export function PhoneSoc({ me, incidentId }: { me: Me; incidentId?: number }) {
  const soc = useSoc();
  const now = useNow(1000);
  const [open, setOpen] = useState<number | null>(incidentId ?? null);
  useEffect(() => { if (incidentId) setOpen(incidentId); }, [incidentId]);
  const { ring } = splitLanes(soc.queue.incidents);
  const mine = ring.filter((i) => i.claimed_by === me.user.id);
  const rest = ring.filter((i) => i.claimed_by !== me.user.id);
  const [busy, setBusy] = useState<number | null>(null);
  const claim = async (i: Incident) => {
    setBusy(i.id);
    try { soc.put(await socApi.claim(i.id)); setOpen(i.id); }
    catch (e) { toast.error(e); soc.refresh(); }
    finally { setBusy(null); }
  };
  if (open != null) return <PhoneSheet me={me} id={open} now={now} onBack={() => setOpen(null)} />;
  const row = (i: Incident) => (
    <li key={i.id} className={`soc-phone-row soc-pri-${i.priority}`}>
      <button className="soc-phone-open" onClick={() => setOpen(i.id)}>
        <span className="row"><span className={priorityClass(i.priority)} aria-label={`${priorityLabel(i.priority)} priority`}>{priorityLabel(i.priority)}</span><SlaPill incident={i} now={now} />
          <span className="muted small">{age(i.opened_at, now)}</span></span>
        <strong>{i.org_name} › {i.location_name}</strong>
        <span className="small">{incidentTitle(i)}{i.claimed_by_email ? <span className="muted"> · {i.claimed_by === me.user.id ? "you" : i.claimed_by_email}</span> : null}</span>
      </button>
      {i.state === "new" && <button className="soc-claim big" disabled={busy != null} onClick={() => claim(i)}>{busy === i.id ? "Claiming…" : "Claim"}</button>}
    </li>
  );
  return (
    <div className="soc-phone">
      <div className="soc-sr" aria-live="polite" role="status">{soc.announcement}</div>
      {soc.escalated.length > 0 && <div className="soc-escalation" role="alert">⚠ {soc.escalated.length} escalated to supervisors</div>}
      {mine.length > 0 && <><h3>Mine</h3><ul className="soc-phone-list">{mine.map(row)}</ul></>}
      <h3>Ringing</h3>
      {rest.length ? <ul className="soc-phone-list">{rest.map(row)}</ul> : <p className="muted">Nothing ringing.</p>}
    </div>
  );
}

function PhoneSheet({ me, id, now, onBack }: { me: Me; id: number; now: number; onBack: () => void }) {
  const soc = useSoc();
  const { detail, incident, error, reload } = useIncident(id);
  const actions = useIncidentActions(reload);
  const events = [...(detail?.events ?? incident?.events ?? [])].sort((a, b) => b.ts - a.ts);
  const back = <a href={socHref()} onClick={(e) => { go(socHref())(e); onBack(); }} className="small">← Queue</a>;
  if (!incident) return <div className="soc-phone">{back}<p className="muted">{error ?? "Loading…"}</p></div>;
  return (
    <div className="soc-phone soc-sheet">
      {back}
      <header>
        <div className="muted small">{incident.org_name} › {incident.location_name}</div>
        <h2>{incidentTitle(incident)} <span className="muted small">#{incident.id}</span></h2>
        <div className="row"><span className={priorityClass(incident.priority)} aria-label={`${priorityLabel(incident.priority)} priority`}>{priorityLabel(incident.priority)}</span><SlaPill incident={incident} now={now} /></div>
      </header>
      <ClaimBar incident={incident} me={me} actions={actions} compact />
      {events[0] && <EventMedia key={`${events[0].server_id}:${events[0].event_id}`} ev={events[0]} />}
      <Dispatch place={detail?.site} siteId={incident.location_id} map />
      {detail && <CallList incident={incident} contacts={detail.contacts} log={detail.log} actions={actions} enabled={incident.state === "claimed" && incident.claimed_by === me.user.id} phone />}
      <ResolvePane me={me} incident={incident} actions={actions} groups={soc.groups} phone onResolved={onBack} />
    </div>
  );
}
