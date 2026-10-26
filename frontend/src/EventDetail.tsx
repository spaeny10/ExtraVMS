import { useCallback, useEffect, useRef, useState } from "react";
import {
  type NamedIdentity,
  api, fmtDuration, fmtTime, fmtWhen, ppeItemsText, type SiteApi,
  type ChatMessage, type ChatRef, type Feedback, type Journey, type NvrEvent, type Synopsis, type Threat, type TimelineEvent, type Verdict, type WeaponCheck,
} from "./api";
import { refChips, refSummary, refTarget, stripHandles } from "./chatRefs";
import { EventPlayer, type EventPlayerHandle } from "./EventPlayer";
import { clipOrigin } from "./eventPlayback";
import { StatusBadge, eventLabel, placeholder, rejectedReason } from "./Events";
import { useNav } from "./nav";
import { ReadPlate } from "./PlateRead";
import { Icon, confirmDialog, toast, useIsPhone } from "./ui";
import { aiModelLabel } from "./aiName";

const REASONS = ["Wrong object", "Missed detail", "Made something up", "Threat too high", "Threat too low", "Too vague"];
const VERDICTS: [Verdict, string][] = [["correct", "Correct detection"], ["false_alarm", "False alarm"], ["wrong_class", "Wrong class"]];
const THREATS: Threat[] = ["none", "low", "medium", "high"];

/**
 * `site` = the API of the server the event lives on (the hub passes one per server; default: this server).
 * `variant` "drawer" docks the viewer to the right edge at full height (the hub's SOC workstation opens it beside the
 * incident it is working on, so the queue stays visible); the default "modal" is the centered dialog every page uses.
 */
export function EventDetail({ id: initialId, cameraName, onClose, site = api, variant = "modal" }: {
  id: number; cameraName: (id: string) => string; onClose: () => void; site?: SiteApi; variant?: "modal" | "drawer";
}) {
  // the viewer can step to another camera's sighting of the same person (Journey), or to another event of the camera
  // on the player's strip (a tick, previous / next)
  const [id, setId] = useState(initialId);
  useEffect(() => setId(initialId), [initialId]);
  const idRef = useRef(id);
  idRef.current = id;
  const [e, setEState] = useState<NvrEvent | null>(null);
  // Only the selected event's record is shown: a late answer for one switched away from (a fetch, a save, feedback) is
  // dropped, so nothing meant for one event lands on the other.
  const setE = useCallback((x: NvrEvent) => { if (x.id === idRef.current) setEState(x); }, []);
  const [chat, setChatState] = useState<ChatMessage[]>([]);
  /** setChat for event `eid`'s conversation: ignored once the viewer shows another event. */
  const chatFor = (eid: number) => (c: ChatMessage[]) => { if (idRef.current === eid) setChatState(c); };
  // The player stays (and keeps playing) when it switched the event itself (onSelectEvent); any other change of event
  // (the page, Journey) or one it asked to start afresh on (no recording there) mounts a new one.
  const sticky = useRef<{ key: number; ids: Set<number> } | null>(null);
  let playerKey = e?.id ?? id;
  if (e) {
    if (sticky.current?.ids.has(e.id)) playerKey = sticky.current.key;
    else sticky.current = { key: e.id, ids: new Set([e.id]) };
  }
  const onSelectEvent = useCallback((x: TimelineEvent, o: { remount: boolean }) => {
    if (!o.remount) sticky.current?.ids.add(x.id);
    setId(x.id);
  }, []);
  const [tab, setTab] = useState<"clip" | "details" | "journey" | "chat">("details");
  const isPhone = useIsPhone();
  // a sighting opened from a clip-chat answer (its chips): the event that conversation is on, to go back to it
  const [askedFrom, setAskedFrom] = useState<number | null>(null);
  /** Show another event in this viewer, as a Journey sighting does: the detail re-keys to it and a new player mounts
   * (it may be on another camera). From an Ask answer the phone shows its clip, a desktop its details. */
  const showEvent = (eid: number) => { sticky.current?.ids.delete(eid); setId(eid); };
  const openRef = (eid: number) => {
    setAskedFrom((from) => from ?? idRef.current);
    showEvent(eid);
    setTab(isPhone ? "clip" : "details");
  };
  useEffect(() => { if (isPhone) setTab("clip"); }, [isPhone]);
  // The player (EventPlayer): its playhead for the Ask tab, and seek for chat frames and notes. A phone shows it on
  // the Clip tab only: coming back there (or tapping a chat frame elsewhere) mounts it again at `resume`.
  const player = useRef<EventPlayerHandle | null>(null);
  const playhead = useRef<Playhead>({ t: null, subs: new Set() });
  const [resume, setResume] = useState<{ t: number; paused: boolean } | null>(null);
  const [footage, setFootage] = useState<boolean | undefined>(undefined);   // is any of the event's footage there? (the player says)
  useEffect(() => { setResume(null); }, [id]);
  useEffect(() => { playhead.current.t = null; }, [playerKey]);   // a new player: no playhead until it reports one
  const onTime = useCallback((t: number) => { playhead.current.t = t; playhead.current.subs.forEach((f) => f(t)); }, []);
  const { openInTimeline } = useNav();

  const gone = () => { toast.info("This sighting was merged into a longer event"); onClose(); };
  useEffect(() => {
    // switched on the player's strip to an event that has gone meanwhile (merged): say so, the viewer stays open
    const fromStrip = sticky.current != null && sticky.current.ids.has(id) && id !== sticky.current.key;
    site.event(id).then(setE).catch(() => (fromStrip ? toast.error("Couldn't load that event") : gone()));
    setChatState([]);
    site.chat(id).then(chatFor(id)).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);
  useEffect(() => {
    const onKey = (k: KeyboardEvent) => k.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  // Poll while the pipeline is still working on this event.
  useEffect(() => {
    if (!e || (e.status !== "open" && e.status !== "pending" && !(e.status === "verified" && !e.synopsis && e.camera_class === "person"))) return;
    const t = setInterval(() => site.event(id).then(setE).catch(gone), 3000);
    return () => clearInterval(t);
  }, [e, id]);

  if (!e) return null;
  // the Journey tab exists only for an event that is part of a journey; a stale selection falls back to Details
  const view = tab === "journey" && !e.journey_id ? "details" : tab;
  const crops = e.detections?.keyframes?.filter((k) => k.kind === "crop") ?? [];
  // chat frames and notes give clip time (seconds from the clip's start): the player works in epoch seconds
  const seek = (t: number) => {
    const at = clipOrigin(e) + t;
    if (player.current) player.current.seek(at, true);
    else { setResume({ t: at, paused: true }); setTab("clip"); }
  };
  const showClip = () => {
    if (view !== "clip" && playhead.current.t != null) setResume({ t: playhead.current.t, paused: false });
    setTab("clip");
  };

  return (
    <div className={`modal-backdrop ${variant === "drawer" ? "drawer" : ""}`} onClick={onClose}>
      <div className={`modal ${variant === "drawer" ? "drawer" : ""}`} onClick={(ev) => ev.stopPropagation()}>
        <header className="modal-head">
          <div>
            <h2>{cameraName(e.camera_id)} · {eventLabel(e)}</h2>
            <span className="muted">{fmtTime(e.start_ts)} · {fmtDuration(e)} · event #{e.id}</span>
            {askedFrom != null && askedFrom !== e.id && (
              <button className="linkish small" title="Back to the event whose Ask answer listed this sighting"
                onClick={() => { showEvent(askedFrom); setAskedFrom(null); setTab("chat"); }}>← Back to event #{askedFrom}</button>
            )}
          </div>
          <div className="row">
            <StatusBadge e={e} />
            <WeaponCheckBadge check={e.synopsis_json?.weapon_check} />
            {e.feedback?.verdict === "false_alarm" && <span className="badge status-error">Marked false alarm</span>}
            <button className="ghost" onClick={() => { openInTimeline(e); onClose(); }} title="Show this event on the Timeline, playing from just before it">⏱ Open in Timeline</button>
            <LockControl key={`lock${e.id}`} e={e} site={site} onChange={() => site.event(e.id).then(setE)} />
            {e.status === "verified" && <WatchControl key={`watch${e.id}`} e={e} site={site} onChange={() => site.event(e.id).then(setE)} />}
            {e.status === "open" || e.status === "pending"
              ? <span className="muted small" title="Verification runs automatically once the event ends">{e.status === "open" ? "Still tracking…" : "Verifying…"}</span>
              : <button className="ghost" onClick={() => site.reprocess(e.id).then(onClose).catch((err) => toast.error(err))} title="Run YOLO verification and the synopsis again">Reprocess</button>}
            <button className="ghost" onClick={onClose} aria-label="Close">✕</button>
          </div>
        </header>
        {isPhone && (
          <div className="segmented detail-tabs">
            <button className={view === "clip" ? "active" : ""} onClick={showClip}>Clip</button>
            <button className={view === "details" ? "active" : ""} onClick={() => setTab("details")}>Details</button>
            {e.journey_id ? <button className={view === "journey" ? "active" : ""} onClick={() => setTab("journey")}>Journey</button> : null}
            <button className={view === "chat" ? "active" : ""} onClick={() => setTab("chat")}>Ask{chat.length ? ` (${chat.filter((m) => m.role === "user").length})` : ""}</button>
          </div>
        )}
        <div className={`modal-grid ${isPhone ? "phone" : ""}`}>
          {(!isPhone || view === "clip") && <div>
            <EventPlayer key={playerKey} e={e} site={site} startAt={resume} onTime={onTime} handle={player} onFootage={setFootage}
              onSelectEvent={onSelectEvent} />
            {crops.length > 0 && (
              <div className="crops">
                {crops.map((k) => <img key={k.file} src={site.media(e, k.file)} alt="" />)}
              </div>
            )}
            {isPhone && <p className="synopsis">{e.synopsis ?? <span className="muted">{placeholder(e)}</span>}</p>}
          </div>}
          {(!isPhone || view !== "clip") && <aside className="detail-side">
            {!isPhone && <div className="segmented">
              <button className={view === "details" ? "active" : ""} onClick={() => setTab("details")}>Details</button>
              {e.journey_id ? <button className={view === "journey" ? "active" : ""} onClick={() => setTab("journey")}
                title="The same person on other cameras">Journey</button> : null}
              <button className={view === "chat" ? "active" : ""} onClick={() => setTab("chat")}>
                Ask about this clip{chat.length ? ` (${chat.filter((m) => m.role === "user").length})` : ""}
              </button>
            </div>}
            {view === "journey" ? (
              <JourneySection key={e.id} e={e} site={site} cameraName={cameraName} onShow={showEvent}
                onOpenTimeline={(members) => { openInTimeline({ ...e, members }); onClose(); }} />
            ) : view === "chat" ? (
              <ChatPanel key={e.id} e={e} site={site} chat={chat} setChat={chatFor(e.id)} playhead={playhead} footage={footage} seek={seek}
                cameraName={cameraName} onOpen={openRef} />
            ) : (
              <Details key={e.id} e={e} site={site} setE={setE} notes={chat.filter((m) => m.saved)} onUnsave={(m) => site.saveNote(e.id, m.id, false).then(chatFor(e.id))} seek={seek} />
            )}
          </aside>}
        </div>
      </div>
    </div>
  );
}

/** Cross-camera journey: the same person seen on neighboring cameras (re-ID + Qwen confirmed). */
function JourneySection({ e, site, cameraName, onShow, onOpenTimeline }: {
  e: NvrEvent; site: SiteApi; cameraName: (id: string) => string; onShow: (id: number) => void;
  onOpenTimeline: (members: { id: number; cam: string; start: number; end: number }[]) => void;
}) {
  const [j, setJ] = useState<Journey | null>(null);
  const load = () => site.eventJourney(e.id).then(setJ).catch(() => setJ(null));
  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [e.id, e.journey_id]);
  if (!j) return null;
  const first = j.events[0]?.start_ts ?? 0;
  const linkInto = (id: number) => j.links.find((l) => l.b === id);
  return (
    <section className="journey">
      <div className="section-head">
        <h3>🔗 Seen on other cameras</h3>
        <button className="ghost small" onClick={() => onOpenTimeline(j.events.map((m) => ({ id: m.id, cam: m.camera_id, start: m.start_ts, end: m.end_ts ?? m.start_ts })))}
          title="Play the whole journey on the Timeline with these cameras side by side">⏱ Open journey on Timeline</button>
      </div>
      <p>{j.synopsis ?? <span className="muted">{j.dirty ? "Otso will describe the whole journey shortly…" : "No journey description."}</span>}</p>
      <ol className="journey-list">
        {j.events.map((m) => {
          const link = linkInto(m.id);
          return (
            <li key={m.id} className={m.id === e.id ? "current" : ""}>
              <button className="journey-thumb" onClick={() => m.id !== e.id && onShow(m.id)} title={m.id === e.id ? "This event" : "Show this sighting"}>
                {m.snapshot ? <img src={site.media(m, "snapshot.jpg", 320)} alt="" /> : <span className="thumb-empty">{m.camera_class}</span>}
              </button>
              <div className="journey-info">
                <div><strong>{cameraName(m.camera_id)}</strong> <span className="muted small">{fmtTime(m.start_ts)}{m.start_ts > first ? ` · +${Math.round(m.start_ts - first)} s` : ""}</span></div>
                {link && (
                  <div className="small muted">
                    Same person ({link.confidence ?? "?"}, re-ID {link.sim.toFixed(2)}, {link.gap_s >= 0 ? `${Math.round(link.gap_s)} s walk` : "overlapping views"}): {link.reason}
                    <button className="linkish" onClick={async () => { await site.rejectLink(link.id); load(); }}>Not the same person</button>
                  </div>
                )}
              </div>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

/** Put the person (or vehicle) in this clip on the watch list: future sightings that match their appearance are
 * raised to medium priority and shown under Needs attention. Names the fingerprint if it has no name yet. */
function WatchControl({ e, site, onChange }: { e: NvrEvent; site: SiteApi; onChange: () => void }) {
  const [ident, setIdent] = useState<(NamedIdentity & { sim: number }) | null | undefined>(undefined);
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [note, setNote] = useState("");
  const [known, setKnown] = useState<NamedIdentity[]>([]);  // so an existing name can be picked (adds this look)
  const isPerson = e.camera_class === "person";
  useEffect(() => { site.eventIdentity(e.id).then(setIdent).catch(() => setIdent(null)); }, [e.id, e.watched]);
  if (ident === undefined) return null;
  const watching = ident?.watch ? ident : null;
  const stop = async () => {
    if (!watching) return;
    await site.watchIdentity(watching.id, false);
    toast.success(`Stopped watching ${watching.name}`);
    onChange();
  };
  const start = async (ev: React.FormEvent) => {
    ev.preventDefault();
    try {
      if (ident) {
        await site.watchIdentity(ident.id, true, note);
        toast.success(`Watching ${ident.name}: matching sightings will be flagged`);
      } else {
        if (!name.trim()) return;
        await site.nameIdentity({ kind: e.camera_class, name: name.trim(), notes: "", event_ids: [e.id], watch: true, watch_note: note });
        toast.success(`Watching "${name.trim()}": matching sightings will be flagged`);
      }
      setOpen(false);
      onChange();
    } catch (err) { toast.error(err); }
  };
  if (watching) {
    return <button className="ghost watch-on" title={`On the watch list${watching.watch_note ? `: ${watching.watch_note}` : ""}. Click to stop.`} onClick={stop}>👁 Watching {watching.name}</button>;
  }
  return (
    <span className="watch-control">
      <button className="ghost" onClick={() => { setOpen(!open); if (!open) site.identities(e.camera_class as "person" | "vehicle", 0).then((x) => setKnown(x.named)).catch(() => {}); }}
        title="Flag future sightings of this person's appearance (clothing, build) as medium priority. Re-ID sees the outfit, not the face: name them again in each outfit.">
        👁 Watch this {isPerson ? "person" : "vehicle"}
      </button>
      {open && (
        <form className="watch-form" onSubmit={start}>
          {ident
            ? <span>Recognized as <strong>{ident.name}</strong>.</span>
            : <><input autoFocus list="known-names-watch" placeholder={isPerson ? "Name (pick a known name to add this look to them)" : "Name, e.g. white van"} value={name} onChange={(ev) => setName(ev.target.value)} />
                <datalist id="known-names-watch">{known.map((n) => <option key={n.id} value={n.name} />)}</datalist></>}
          <input placeholder="Why (optional, shown with each sighting)" value={note} onChange={(ev) => setNote(ev.target.value)} />
          <button type="submit" className="small" disabled={!ident && !name.trim()}>Watch</button>
          <button type="button" className="ghost small" onClick={() => setOpen(false)}>Cancel</button>
          <span className="muted small">Matches by appearance, not face; confirm sightings by naming them to sharpen it.</span>
        </form>
      )}
    </span>
  );
}

/** The description mentioned a weapon: what the second, full-resolution look decided (backend/nvr/weaponcheck.py). */
const EMPTY_HANDS = ["nothing", "none", "empty", "empty hands", "hands empty", "nothing visible", "no object"];

function weaponCheckText(c: WeaponCheck): { label: string; cls: string } {
  const kind = c.kind === "knife" ? "knife" : c.kind === "firearm" ? "firearm" : "weapon";
  const objects = [...new Set((c.persons ?? []).flatMap((p) => (p.held?.length ? p.held : [p.object]))
    .filter((o) => o && !EMPTY_HANDS.includes(o)))];
  if (c.verdict === "confirmed") return { label: `${kind[0].toUpperCase()}${kind.slice(1)} confirmed`, cls: "confirmed" };
  if (c.verdict === "not_confirmed") return { label: `No weapon${objects.length ? `: ${objects.join(", ")}` : ""}`, cls: "cleared" };
  return { label: `Possible ${kind}, unconfirmed — needs a person to look`, cls: "unconfirmed" };
}

function WeaponCheckBadge({ check }: { check?: WeaponCheck | null }) {
  if (!check?.verdict) return null;
  const { label, cls } = weaponCheckText(check);
  return <span className={`badge weapon-check ${cls}`} title={`Second look at full resolution: ${check.reason}`}>{label}</span>;
}

/**
 * The two "originals" an event can carry. `correction`: the model's version an operator corrected (events.synopsis_original
 * with corrected_at set): shown with "Revert to original". `weaponClaim`: what the model wrote before the weapon check
 * changed it (weapon_check.original; older events kept it in synopsis_original without corrected_at): read-only, never
 * revertable, since reverting would bring back the unchecked claim (event 9118's "black handgun").
 */
export function synopsisOriginals(e: Pick<NvrEvent, "synopsis_json" | "synopsis_original" | "corrected_at">):
  { correction: Synopsis | null; weaponClaim: Synopsis | null } {
  const check = e.synopsis_json?.weapon_check as (WeaponCheck & { original?: Synopsis | null }) | null | undefined;
  const correction = e.corrected_at && e.synopsis_original ? e.synopsis_original : null;
  const legacy = !e.corrected_at && check && check.verdict !== "confirmed" ? e.synopsis_original ?? null : null;
  return { correction, weaponClaim: check?.original ?? legacy };
}

function WeaponCheckLine({ check, original }: { check: WeaponCheck; original?: Synopsis | null }) {
  const { label, cls } = weaponCheckText(check);
  return (
    <div className={`d-line weapon-line ${cls}`} title="The description mentioned a weapon, so every person in the clip was looked at again at full camera resolution before the event was rated">
      <strong>Weapon check:</strong> {label}. <span className="muted small">{check.reason}{check.model ? ` · ${aiModelLabel(check.model)}` : ""}</span>
      {original?.summary ? (
        <div className="muted small">Before the check, the description said: {original.summary} <em>(threat: {original.threat_level})</em></div>
      ) : null}
    </div>
  );
}

/** Lock keeps this event's footage (with padding) forever, exempt from retention. */
function LockControl({ e, site, onChange }: { e: NvrEvent; site: SiteApi; onChange: () => void }) {
  const [asking, setAsking] = useState(false);
  const [note, setNote] = useState("");
  if (e.lock) {
    return (
      <button className="ghost lock-on" title={`Locked${e.lock.note ? `: ${e.lock.note}` : ""}. Footage is kept until unlocked.`}
        onClick={async () => {
          if (!await confirmDialog("Unlock this event's footage?", { message: "It will follow the normal retention policy again.", confirmLabel: "Unlock", danger: true })) return;
          await site.unlockEvent(e.id); onChange(); toast.success("Unlocked");
        }}>
        <Icon name="lock" size={14} /> Locked
      </button>
    );
  }
  if (asking) {
    return (
      <form className="row lock-form" onSubmit={(ev) => { ev.preventDefault(); site.lockEvent(e.id, note).then(() => { setAsking(false); onChange(); }); }}>
        <input autoFocus placeholder="Reason (optional), e.g. police report #123" value={note} onChange={(ev) => setNote(ev.target.value)} />
        <button type="submit">Lock</button>
        <button type="button" className="ghost" onClick={() => setAsking(false)}>Cancel</button>
      </form>
    );
  }
  return <button className="ghost" onClick={() => setAsking(true)} title="Keep this footage forever, regardless of retention">🔓 Lock</button>;
}

/* ------------------------------------------------------------------ details tab */

function Details({ e, site, setE, notes, onUnsave, seek }: {
  e: NvrEvent; site: SiteApi; setE: (e: NvrEvent) => void; notes: ChatMessage[]; onUnsave: (m: ChatMessage) => void; seek: (t: number) => void;
}) {
  const [editing, setEditing] = useState(false);
  const s = e.synopsis_json;
  const originals = synopsisOriginals(e);
  const canGenerate = Boolean(e.detections?.keyframes?.length);
  // Qwen's "activity" line usually restates the summary; show it only when it adds something
  const activity = s?.activity && !(e.synopsis ?? "").toLowerCase().includes(s.activity.toLowerCase().slice(0, 40)) ? s.activity : null;
  const tags = s?.tags ?? [];
  const frames = e.detections?.samples?.length ?? 0;
  const ppe = e.detections?.ppe;
  const rules = e.rules?.length ? [...new Set(e.rules.map((r) => r.topic.split("/").slice(-2, -1)[0] || r.topic))] : [];

  return (
    <>
      <section className="d-section">
        <div className="section-head">
          <h3>Synopsis {e.corrected_at ? <span className="badge">Corrected</span> : null}</h3>
          <div className="row">
            {s && !editing && <button className="ghost small" onClick={() => setEditing(true)}>Edit</button>}
            {!s && !editing && <button className="ghost small" onClick={() => setEditing(true)}>Write one</button>}
            {canGenerate && !editing && (
              <button className="ghost small" disabled={e.synopsis_pending} onClick={async () => {
                if (e.corrected_at && !await confirmDialog("Replace your corrected synopsis?", { message: "Otso will write a new one; your correction is lost.", confirmLabel: "Regenerate", danger: true })) return;
                await site.generateSynopsis(e.id); toast.info("Otso is writing a new synopsis…"); site.event(e.id).then(setE);
              }}>{e.synopsis_pending ? "Otso is writing…" : s ? "Regenerate" : "Generate with Otso"}</button>
            )}
          </div>
        </div>
        {editing ? (
          <SynopsisEditor initial={s ?? blankSynopsis(e)} onCancel={() => setEditing(false)} onSave={async (next) => {
            setE(await site.correctSynopsis(e.id, next));
            setEditing(false);
          }} />
        ) : (
          <>
            <p className="d-summary">{e.synopsis ?? <span className="muted">{placeholder(e)}</span>}</p>
            {e.areas?.length ? (
              <div className="d-line" title="Named places the person walked into, in order">📍 {e.areas.map((a) => `${a.name} (+${Math.max(0, Math.round(a.from - e.start_ts))} s)`).join(" → ")}</div>
            ) : null}
            {e.camera_class === "vehicle" && e.status === "verified" ? (
              <div className="d-line plate-line" title="Read by Otso from the sharpest full-resolution frames of this vehicle, only when the plate is clearly legible">
                🔢 <ReadPlate key={`${e.id}/${e.plate ?? ""}`} id={e.id} plate={e.plate} region={e.plate_region} site={site}
                  onRead={(_r, next) => { if (next) setE(next); }} />
              </div>
            ) : null}
            {e.ptz_preset ? (
              <div className="d-line" title="Zones, named places, painted regions and the learned baseline describe the home view and were not applied">↗ Camera was turned away from its home view{e.ptz_preset !== "away" ? ` (at preset '${e.ptz_preset}')` : ""}</div>
            ) : null}
            {e.policy && e.policy.kind !== "ppe" ? (
              <div className="d-line rule-broken" title="A site rule from Settings → Cameras → Site rules">🚫 <strong>Site rule:</strong> {e.policy.text}</div>
            ) : null}
            {ppe && ["violation", "compliant", "unclear"].includes(ppe.verdict) ? (
              <div className={`d-line ${ppe.verdict === "violation" ? "rule-broken" : ""}`} title="PPE zone from Settings → Cameras → Zones. The PPE detector checks the recorded frames; Otso looks when it is unsure.">
                🦺 <strong>PPE in '{ppe.zone}':</strong> {ppe.verdict === "violation" ? (e.policy?.kind === "ppe" ? e.policy.text : "violation") : ppe.verdict === "compliant" ? "required items worn" : "could not tell"}
                <span className="muted small"> · {ppeItemsText(ppe.items, ppe.required)}{ppe.marked ? <> · <a href={site.media(e, ppe.marked)} target="_blank" rel="noreferrer">marked frame</a></> : null}</span>
              </div>
            ) : null}
            {e.anomaly_json?.reasons?.length ? (
              <div className="d-line unusual-why" title="From what this camera normally sees (learned from the last 4 weeks)">
                ⚠ {e.anomaly_json.reasons.join("; ")}{e.priority && e.priority !== "none" ? ` · priority ${e.priority}` : ""}
              </div>
            ) : null}
            {s?.weapon_check ? <WeaponCheckLine check={s.weapon_check} original={originals.weaponClaim} /> : null}
            {s?.threat_reason && s.threat_level !== "none" && <div className="d-line"><strong>Threat ({s.threat_level}):</strong> {s.threat_reason}</div>}
            {tags.length > 0 && <div className="tags">{tags.slice(0, 8).map((t) => <span key={t} className="tag">{t}</span>)}{tags.length > 8 && <span className="tag muted">+{tags.length - 8}</span>}</div>}
            {(activity || s?.objects?.length || s?.model || originals.correction) && (
              <details className="d-more">
                <summary className="muted small">More from Otso</summary>
                {activity && <p className="muted">{activity}</p>}
                {s?.objects?.length ? (
                  <ul className="plain small">
                    {s.objects.map((o, i) => <li key={i}><strong>{o.type}</strong> — {o.description}</li>)}
                  </ul>
                ) : null}
                {s?.model && <p className="muted small model-tag">written by {aiModelLabel(s.model)}</p>}
                {originals.correction && (
                  <div className="original small">
                    <p className="muted">Otso's original: {originals.correction.summary} <em>(threat: {originals.correction.threat_level})</em></p>
                    <button className="ghost small" onClick={async () => setE(await site.revertSynopsis(e.id))}>Revert to original</button>
                  </div>
                )}
              </details>
            )}
          </>
        )}
      </section>

      <FeedbackBar e={e} site={site} setE={setE} />

      {notes.length > 0 && (
        <section className="d-section">
          <h3>Notes</h3>
          {notes.map((m) => (
            <div key={m.id} className="note">
              <p>{m.content}</p>
              <div className="row small muted">
                {m.at != null && <button className="linkish" onClick={() => seek(m.at!)}>at {m.at.toFixed(1)}s</button>}
                <button className="linkish" onClick={() => onUnsave(m)}>Remove note</button>
              </div>
            </div>
          ))}
        </section>
      )}

      <section className="d-section">
        <details className="d-more">
          <summary>
            <h3 className="inline">Verification</h3>
            <span className="muted small"> camera {e.camera_class} {((e.camera_conf ?? 0) * 100).toFixed(0)}% · YOLO {e.yolo_class ?? "—"}{e.yolo_conf != null ? ` ${(e.yolo_conf * 100).toFixed(0)}%` : ""} · {e.yolo_hits ?? 0}/{frames} frames agree</span>
          </summary>
          <table className="kv">
            <tbody>
              <tr><td>Camera</td><td>{e.camera_class} · {((e.camera_conf ?? 0) * 100).toFixed(0)}% · track {e.track_id}</td></tr>
              {e.tracks && e.tracks.length > 1 && <tr><td>Merged</td><td>
                {e.tracks.length} camera tracks{e.people && e.people > 1 ? `, up to ${e.people} people at once` : ""}
                {e.tracks.filter((m) => m.why).map((m) => <div key={m.event} className="muted small">{m.event}: {m.why}</div>)}
              </td></tr>}
              {e.detections?.source === "onvif_event" && <tr><td>Source</td><td>the camera's ONVIF event (no object positions): YOLO found the {e.camera_class} in the clip</td></tr>}
              <tr><td>YOLO</td><td>{e.yolo_class ?? "—"} {e.yolo_conf != null && `· ${(e.yolo_conf * 100).toFixed(0)}%`}</td></tr>
              <tr><td>Agreement</td><td>{e.yolo_hits ?? 0} of {frames} frames (need {e.detections?.needed ?? "—"}){e.detections?.time_shift_s ? ` · clock shift ${e.detections.time_shift_s} s` : ""}</td></tr>
              {e.detections?.rejected && <tr><td>Rejected</td><td>{rejectedReason(e)}{e.detections.parked ? ` · ${e.detections.parked.cls} sat still ${e.detections.parked.via === "memory" ? "on a remembered parking spot" : "while the camera's boxes were small movements on it"}` : ""}</td></tr>}
              {rules.length > 0 && <tr><td>Rules</td><td>{rules.join(", ")}</td></tr>}
              {ppe && <tr><td>PPE check</td><td>
                {ppe.error ? `${ppe.verdict}: ${ppe.error}` : <>
                  detector: {ppeItemsText(ppe.detector, ppe.required)}
                  {ppe.vlm ? <> · Otso: {ppeItemsText(ppe.vlm, ppe.required)}{ppe.vlm.head ? ` (head: ${ppe.vlm.head})` : ""}</> : " · Otso not needed"}
                  {ppe.overruled?.length ? " · Otso changed the detector's answer" : ""} · {ppe.dwell_s.toFixed(0)} s in zone
                </>}
              </td></tr>}
            </tbody>
          </table>
        </details>
      </section>
    </>
  );
}

/** One compact row: rate the synopsis, judge the detection, add a note. Detail appears only when needed. */
function FeedbackBar({ e, site, setE }: { e: NvrEvent; site: SiteApi; setE: (e: NvrEvent) => void }) {
  const fb: Feedback = e.feedback ?? {};
  const [note, setNote] = useState(fb.note ?? "");
  const [noteOpen, setNoteOpen] = useState(Boolean(fb.note));
  const send = async (patch: Feedback) => { setE(await site.feedback(e.id, patch)); toast.success("Feedback saved"); };
  const toggleReason = (r: string) => {
    const reasons = new Set(fb.reasons ?? []);
    if (reasons.has(r)) reasons.delete(r);
    else reasons.add(r);
    send({ reasons: [...reasons] });
  };
  return (
    <section className="d-section feedback">
      <div className="fb-row">
        <h3 className="inline">Feedback</h3>
        {e.synopsis && (
          <span className="fb-group" title="Was the synopsis right?">
            <button className={`ghost small ${fb.rating === "up" ? "on" : ""}`} onClick={() => send({ rating: fb.rating === "up" ? null : "up" })} aria-label="Good synopsis">👍</button>
            <button className={`ghost small ${fb.rating === "down" ? "on" : ""}`} onClick={() => send({ rating: fb.rating === "down" ? null : "down" })} aria-label="Bad synopsis">👎</button>
          </span>
        )}
        <span className="fb-group" title="Was the detection right?">
          {VERDICTS.map(([v, label]) => (
            <button key={v} className={`ghost small ${fb.verdict === v ? "on" : ""}`} onClick={() => send({ verdict: fb.verdict === v ? null : v })}>{label}</button>
          ))}
        </span>
        {!noteOpen && <button className="linkish small" onClick={() => setNoteOpen(true)}>Add note</button>}
      </div>
      {fb.rating === "down" && (
        <div className="chips">
          {REASONS.map((r) => (
            <button key={r} className={`chip ${fb.reasons?.includes(r) ? "on" : ""}`} onClick={() => toggleReason(r)}>{r}</button>
          ))}
        </div>
      )}
      {fb.verdict === "wrong_class" && (
        <div className="row small">
          <span className="muted">Actually a</span>
          <select value={fb.correct_class ?? ""} onChange={(ev) => send({ correct_class: ev.target.value || null })}>
            <option value="">choose…</option>
            {["person", "vehicle", "animal", "shadow / light", "vegetation", "other"].map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        </div>
      )}
      {noteOpen && (
        <input className="grow" autoFocus={!fb.note} placeholder="Note (searchable)" value={note} onChange={(ev) => setNote(ev.target.value)}
          onBlur={() => note !== (fb.note ?? "") && send({ note })} onKeyDown={(ev) => ev.key === "Enter" && (ev.target as HTMLInputElement).blur()} />
      )}
    </section>
  );
}

function blankSynopsis(e: NvrEvent): Synopsis {
  return { summary: "", activity: "", objects: [{ type: e.yolo_class ?? e.camera_class, description: "" }], threat_level: "none", threat_reason: "", tags: [] };
}

function SynopsisEditor({ initial, onSave, onCancel }: { initial: Synopsis; onSave: (s: Synopsis) => Promise<void>; onCancel: () => void }) {
  const [s, setS] = useState<Synopsis>({ ...initial, objects: initial.objects.map((o) => ({ ...o })) });
  const [tags, setTags] = useState(initial.tags.join(", "));
  const [busy, setBusy] = useState(false);
  const setObj = (i: number, patch: Partial<Synopsis["objects"][number]>) =>
    setS({ ...s, objects: s.objects.map((o, j) => (j === i ? { ...o, ...patch } : o)) });
  const save = async () => {
    setBusy(true);
    try {
      await onSave({ ...s, tags: tags.split(",").map((t) => t.trim()).filter(Boolean), objects: s.objects.filter((o) => o.type.trim()) });
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="form">
      <label className="field"><span>Summary</span><textarea rows={4} value={s.summary} onChange={(ev) => setS({ ...s, summary: ev.target.value })} /></label>
      <label className="field"><span>Activity</span><input value={s.activity} onChange={(ev) => setS({ ...s, activity: ev.target.value })} /></label>
      <div className="field">
        <span>Objects</span>
        {s.objects.map((o, i) => (
          <div key={i} className="row obj-row">
            <input className="obj-type" value={o.type} placeholder="type" onChange={(ev) => setObj(i, { type: ev.target.value })} />
            <input className="obj-desc" value={o.description} placeholder="description" onChange={(ev) => setObj(i, { description: ev.target.value })} />
            <button className="ghost small" onClick={() => setS({ ...s, objects: s.objects.filter((_, j) => j !== i) })} aria-label="Remove">✕</button>
          </div>
        ))}
        <button className="ghost small" onClick={() => setS({ ...s, objects: [...s.objects, { type: "", description: "" }] })}>Add object</button>
      </div>
      <div className="row">
        <label className="field"><span>Threat</span>
          <select value={s.threat_level} onChange={(ev) => setS({ ...s, threat_level: ev.target.value as Threat })}>
            {THREATS.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
        </label>
        <label className="field grow"><span>Threat reason</span><input value={s.threat_reason ?? ""} onChange={(ev) => setS({ ...s, threat_reason: ev.target.value })} /></label>
      </div>
      <label className="field"><span>Tags (comma separated)</span><input value={tags} onChange={(ev) => setTags(ev.target.value)} /></label>
      <p className="muted small">Your correction replaces the synopsis in the UI and search. Recent corrections from this camera are shown to Otso as examples for future events.</p>
      <div className="row">
        <button onClick={save} disabled={busy || !s.summary.trim()}>{busy ? "Saving…" : "Save correction"}</button>
        <button className="ghost" onClick={onCancel}>Cancel</button>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ chat tab */

const SUGGESTIONS = ["What is the person doing?", "Describe their clothing and anything they carry.", "Which way did they come from and leave?", "Is anything suspicious here?",
  "How many times did they show up that day?"];

/** The player's playhead (epoch seconds; null before it plays), shared without re-rendering the whole viewer. */
type Playhead = { t: number | null; subs: Set<(t: number) => void> };

function ChatPanel({ e, site, chat, setChat, playhead, footage, seek, cameraName, onOpen }: {
  e: NvrEvent; site: SiteApi; chat: ChatMessage[]; setChat: (c: ChatMessage[]) => void;
  playhead: React.RefObject<Playhead>; footage: boolean | undefined; seek: (t: number) => void;
  cameraName: (id: string) => string; onOpen: (eventId: number) => void;
}) {
  const [q, setQ] = useState("");
  const [atMoment, setAtMoment] = useState(false);
  // clip time of the playhead (what `at` and the frames' t count in): it may be before the clip, or after it
  const t0 = clipOrigin(e);
  const [now, setNow] = useState(() => (playhead.current.t ?? t0) - t0);
  const [pending, setPending] = useState<{ q: string; at: number | null; answer: string; frames: { file: string; t: number }[]; refs: ChatRef[] } | null>(null);
  const [err, setErr] = useState("");
  const end = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const subs = playhead.current.subs;
    const tick = (t: number) => setNow(t - t0);
    subs.add(tick);
    return () => { subs.delete(tick); };
  }, [playhead, t0]);
  useEffect(() => {
    // Block body on purpose: newer browsers return a Promise from scrollIntoView, and React
    // would try to call an effect's return value as its cleanup.
    end.current?.scrollIntoView({ block: "end" });
  }, [chat, pending]);

  const ask = async (text: string) => {
    if (!text.trim() || pending) return;
    const at = atMoment ? Math.round(now * 10) / 10 : null;
    setErr("");
    setQ("");
    setPending({ q: text, at, answer: "", frames: [], refs: [] });
    try {
      await site.askClip(e.id, text, at, (c) => {
        if (c.type === "frames") setPending((p) => p && { ...p, frames: c.frames });
        if (c.type === "refs") setPending((p) => p && { ...p, refs: c.refs });
        if (c.type === "delta") setPending((p) => p && { ...p, answer: p.answer + c.text });
        if (c.type === "error") setErr(c.error);
      });
    } catch (ex) {
      setErr(String(ex));
    }
    setChat(await site.chat(e.id));
    setPending(null);
  };

  return (
    <div className="chat">
      {/* the server reads frames from the clip, else from the recording; neither is there: questions beyond the clip
          (backend clipask.py) still have the event search to answer from */}
      {footage === false && <p className="muted small">This event's clip and the recording around it are no longer kept, so questions about
        what it shows can't be answered. Questions beyond it (how many times, where else, what happened before) still can.</p>}
      <div className="chat-log">
        {chat.length === 0 && !pending && (
          <div className="muted small">
            <p>Ask Otso about this clip. It looks at frames spread across the event, or with <em>focus on current moment</em> at frames around the video's playhead.
              Questions beyond the clip (how many times they came today, where else they went, what happened before) search this server's other events too.</p>
            <div className="chips">
              {SUGGESTIONS.map((s) => <button key={s} className="chip" onClick={() => ask(s)}>{s}</button>)}
            </div>
          </div>
        )}
        {chat.map((m) => (
          <Bubble key={m.id} role={m.role} text={m.content} at={m.at} frames={m.frames} e={e} site={site} seek={seek}
            refs={m.refs} cameraName={cameraName} onOpen={onOpen}
            action={m.role === "assistant" ? (
              <button className="linkish" onClick={() => site.saveNote(e.id, m.id, !m.saved).then(setChat)}>{m.saved ? "Saved as note ✓" : "Save as note"}</button>
            ) : null} />
        ))}
        {pending && (
          <>
            <Bubble role="user" text={pending.q} at={pending.at} frames={[]} e={e} site={site} seek={seek} />
            <Bubble role="assistant" at={null} frames={pending.frames} e={e} site={site} seek={seek}
              refs={pending.refs} cameraName={cameraName} onOpen={onOpen}
              text={pending.answer || (pending.refs.length ? "Reading the search results…" : pending.frames.length ? "Looking at the frames…" : "Pulling frames from the clip…")} />
          </>
        )}
        {err && <p className="error small">{err}</p>}
        <div ref={end} />
      </div>
      <form className="chat-input" onSubmit={(ev) => { ev.preventDefault(); ask(q); }}>
        <label className="row small muted">
          <input type="checkbox" checked={atMoment} onChange={(ev) => setAtMoment(ev.target.checked)} />
          Focus on current moment ({now.toFixed(1)}s)
        </label>
        <div className="row">
          <input className="grow" value={q} onChange={(ev) => setQ(ev.target.value)} placeholder="Ask about this clip…" disabled={Boolean(pending)} />
          <button type="submit" disabled={!q.trim() || Boolean(pending)}>Ask</button>
        </div>
        {chat.some((m) => !m.saved) && !pending && (
          <button type="button" className="linkish small" onClick={() => site.clearChat(e.id).then(setChat)}>Clear conversation (keeps saved notes)</button>
        )}
      </form>
    </div>
  );
}

function Bubble({ role, text, at, frames, e, site, seek, action, refs, cameraName, onOpen }: {
  role: string; text: string; at: number | null; frames: { file: string; t: number }[]; e: NvrEvent; site: SiteApi; seek: (t: number) => void; action?: React.ReactNode;
  refs?: ChatRef[]; cameraName?: (id: string) => string; onOpen?: (eventId: number) => void;
}) {
  return (
    <div className={`bubble ${role}`}>
      {at != null && role === "user" && <button className="linkish small" onClick={() => seek(at)}>@ {at.toFixed(1)}s</button>}
      <p>{role === "assistant" ? stripHandles(text) : text}</p>
      {frames.length > 0 && (
        <div className="bubble-frames">
          {frames.map((f) => (
            <img key={f.file} src={site.media(e, f.file)} alt={`t=${f.t}s`} title={`Jump to ${f.t.toFixed(1)}s`} onClick={() => seek(f.t)} />
          ))}
        </div>
      )}
      {refs?.length && cameraName && onOpen ? <RefChips refs={refs} currentId={e.id} site={site} cameraName={cameraName} onOpen={onOpen} /> : null}
      {action && <div className="small">{action}</div>}
    </div>
  );
}

/** The sightings an answer beyond the clip was written from (chatRefs.ts): snapshot, match, time and camera; a tap
 * switches the viewer to that event. */
function RefChips({ refs, currentId, site, cameraName, onOpen }: {
  refs: ChatRef[]; currentId: number; site: SiteApi; cameraName: (id: string) => string; onOpen: (eventId: number) => void;
}) {
  const chips = refChips(refs, currentId, cameraName, (ts) => fmtWhen(ts));
  const head = refSummary(refs);
  return (
    <div className="chat-refs">
      {head && <span className="muted small">{head}</span>}
      <div className="chat-refs-row">
        {chips.map((c) => (
          <button key={c.key} type="button" className={`chat-ref ${c.ref.match}${c.current ? " current" : ""}`} title={c.title}
            aria-label={c.title} disabled={c.current} onClick={() => { const t = refTarget(c.ref, currentId); if (t != null) onOpen(t); }}>
            {c.ref.snapshot
              ? <img src={site.media({ id: c.ref.event_id }, "snapshot.jpg", 320)} alt="" loading="lazy" />
              : <span className="thumb-empty">{c.ref.label ?? "event"}</span>}
            <span className="chat-ref-tag">{c.label}</span>
            <span>{c.time}</span>
            <span className="chat-ref-cam">{c.camera}</span>
          </button>
        ))}
      </div>
    </div>
  );
}
