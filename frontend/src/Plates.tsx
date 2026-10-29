/**
 * Plates (backend traffic.py, traffic_api.py): what this server's traffic cameras read themselves and pushed here.
 *  - Reads: search a plate or part of one (near plates, one character off, come back as "Possible: …" chips that search
 *    them), filters, the reads newest first with their plate and vehicle crops, "Load more" by before_id; a read opens
 *    a panel with the larger picture and the other reads of that vehicle's pass, and its event when it has one. The
 *    camera's reader misses plates its own crops show (cam8: plates ~60 px wide), so the panel has Read plate: Otso
 *    reads the vehicle crop (operator), its answer shown and kept beside the camera's, and searchable. A read the camera
 *    sent without a vehicle picture shows one cut from the recording (the server does it, marked "From the recording"),
 *    and Timeline opens the recording at the read's moment to check it.
 *  - Lists: allow / deny / watch plates with a note and an optional expiry. Deny and watch hits raise an alarm event
 *    (deny high, watch medium); allow only marks the read. Changes need the operator role (a viewer gets the 403).
 *  - Traffic: vehicles per hour (24 h) and per day (7 days), all of them and those with a plate, and each camera's push
 *    health (a camera set up and silent past silent_after_s is flagged, as the server's traffic_push_silent alert is).
 * The camera's state and direction are hints, shown as "reported". Most reads have no plate (rear plates only in
 * Kansas). The hub's Site › Plates renders this over the Site's servers (`sources`, a picker when there are several).
 * Pure logic: traffic.ts.
 */
import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { api as localSite, fmtTime, fmtWhen, type Camera, type SiteApi } from "./api";
import { useNav } from "./nav";
import { EventDetail } from "./EventDetail";
import { formatPlate, plateError } from "./plate";
import {
  type Bar, type Direction, type ListEntry, type Period, type PlateList, type Read, type ReadDetail, type TrafficStatus, PERIODS, PLATE_LISTS,
  VEHICLE_COLORS, VEHICLE_TYPES, confBand, confText, countBars, countsSummary, dateFromExpiry, directionText, expiryFromDate, expiryText,
  filterOptions, isSilent, knownPlate, listBadge, listError, listName, nearChip, onList, otsoText, periodSince, plateProblem, plateText, pushText,
  stateText, timeAgo, vehicleText,
} from "./traffic";
import { Icon, confirmDialog, errorText, promptDialog, toast } from "./ui";

/** A server the page can show (the hub: each of the Site's servers with traffic cameras). */
export type PlatesSource = { id: string; name: string; api: SiteApi };
/** The recording at a read's moment: which source, camera and time (the hub opens its Site Timeline). */
export type PlatesMoment = { source: string; camera: string; ts: number };
/** An event to open: on which source, and the camera names of that server. */
export type PlatesEvent = { source: string; id: number; cameraName: (id: string) => string; onClose: () => void };
type Section = "reads" | "lists" | "traffic";
const PAGE = 50;
const STATUS_POLL_MS = 30000;

export function PlatesView({ site = localSite, sources, renderEvent, openTimeline }: {
  /** the server (default: this one) */
  site?: SiteApi;
  /** several servers (the hub's Site): a picker; the first is shown first */
  sources?: PlatesSource[];
  /** how an event opens (the hub: its own viewer for that server); default the server UI's EventDetail */
  renderEvent?: (ev: PlatesEvent) => ReactNode;
  /** how the recording at a read's moment opens (the hub: its Site Timeline); default this server UI's Timeline */
  openTimeline?: (m: PlatesMoment) => void;
}) {
  const [src, setSrc] = useState(sources?.[0]?.id ?? "");
  const picked = sources?.find((s) => s.id === src) ?? sources?.[0];
  const a = picked?.api ?? site;
  const [section, setSection] = useState<Section>("reads");
  return (
    <div className="view plates">
      <div className="toolbar plates-head">
        <div className="segmented">
          <button className={section === "reads" ? "active" : ""} onClick={() => setSection("reads")}><Icon name="car" size={16} /> Reads</button>
          <button className={section === "lists" ? "active" : ""} onClick={() => setSection("lists")}><Icon name="events" size={16} /> Lists</button>
          <button className={section === "traffic" ? "active" : ""} onClick={() => setSection("traffic")}><Icon name="timeline" size={16} /> Traffic</button>
        </div>
        {sources && sources.length > 1 && (
          <select value={picked?.id ?? ""} onChange={(e) => setSrc(e.target.value)} aria-label="Server">
            {sources.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
          </select>
        )}
      </div>
      {/* keyed by server: nothing of one server's search or lists stays on screen for another */}
      <PlatesBody key={picked?.id ?? ""} a={a} section={section} source={picked?.id ?? ""} renderEvent={renderEvent} openTimeline={openTimeline} />
    </div>
  );
}

function PlatesBody({ a, section, source, renderEvent, openTimeline }: {
  a: SiteApi; section: Section; source: string; renderEvent?: (ev: PlatesEvent) => ReactNode; openTimeline?: (m: PlatesMoment) => void;
}) {
  const nav = useNav();
  // this server UI: its own Timeline, focused on a few seconds around the read (id 0 = a moment, not an event)
  const toTimeline = (camera: string, ts: number) => (openTimeline ? openTimeline({ source, camera, ts })
    : nav.openInTimeline({ id: 0, camera_id: camera, start_ts: ts - 3, end_ts: ts + 3, camera_class: "plate read" }));
  const [cameras, setCameras] = useState<Camera[]>([]);
  const [status, setStatus] = useState<TrafficStatus | null>(null);
  const [statusErr, setStatusErr] = useState("");
  useEffect(() => {
    let alive = true;
    a.cameras().then((c) => { if (alive) setCameras(c); }).catch(() => {});
    const pull = () => a.trafficStatus().then((s) => { if (alive) { setStatus(s); setStatusErr(""); } })
      .catch((e) => { if (alive) setStatusErr(errorText(e)); });
    pull();
    const t = setInterval(pull, STATUS_POLL_MS);
    return () => { alive = false; clearInterval(t); };
  }, [a]);
  const name = (id: string) => status?.cameras.find((c) => c.camera_id === id)?.name ?? cameras.find((c) => c.id === id)?.name ?? id;
  const [eventId, setEventId] = useState<number | null>(null);
  const closeEvent = () => setEventId(null);

  return <>
    {statusErr && <p className="error small">{statusErr}</p>}
    {status && status.cameras.length === 0 && (
      <p className="muted small">No traffic cameras on this server yet. Set one up under Settings › Cameras › (the camera) › Camera › Traffic.</p>
    )}
    {section === "reads" && <ReadsSection a={a} status={status} name={name} onEvent={setEventId} onTimeline={toTimeline} />}
    {section === "lists" && <ListsSection a={a} name={name} />}
    {section === "traffic" && <TrafficSection a={a} status={status} />}
    {eventId !== null && (renderEvent ? renderEvent({ source, id: eventId, cameraName: name, onClose: closeEvent })
      : <EventDetail id={eventId} site={a} cameraName={name} onClose={closeEvent} />)}
  </>;
}

// ---------------------------------------------------------------- reads

type Filters = {
  q: string; camera: string; period: Period; type: string; color: string; direction: "" | Direction; list: "" | PlateList | "any";
  plates: boolean; min_conf: number; noise: boolean;
};
const NO_FILTERS: Filters = { q: "", camera: "", period: "24h", type: "", color: "", direction: "", list: "", plates: false, min_conf: 0, noise: false };

function ReadsSection({ a, status, name, onEvent, onTimeline }: {
  a: SiteApi; status: TrafficStatus | null; name: (id: string) => string; onEvent: (id: number) => void;
  onTimeline: (camera: string, ts: number) => void;
}) {
  const [f, setF] = useState<Filters>(NO_FILTERS);
  const [typed, setTyped] = useState("");
  const [reads, setReads] = useState<Read[] | null>(null);
  const [near, setNear] = useState<string[]>([]);
  const [more, setMore] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [open, setOpen] = useState<number | null>(null);
  // only the latest request's answer lands: a slow first page never replaces the one the filters ask for now
  const seq = useRef(0);
  const set = (p: Partial<Filters>) => setF((x) => ({ ...x, ...p }));

  // typing searches after a pause; Enter at once
  useEffect(() => {
    const q = typed.trim();
    const t = setTimeout(() => setF((x) => (x.q === q ? x : { ...x, q })), 400);   // unchanged: no new search
    return () => clearTimeout(t);
  }, [typed]);

  const load = async (before?: number) => {
    const n = ++seq.current;
    setBusy(true);
    try {
      const r = await a.trafficReads({
        q: f.q || undefined, camera: f.camera || undefined, since: periodSince(f.period), type: f.type || undefined, color: f.color || undefined,
        direction: f.direction || undefined, list: f.list || undefined, plates: f.plates, min_conf: f.min_conf || undefined,
        noise: f.noise, before_id: before, limit: PAGE,
      });
      if (n !== seq.current) return;
      setReads((cur) => (before && cur ? [...cur, ...r.reads] : r.reads));
      if (!before) setNear(r.near);
      setMore(r.reads.length >= PAGE);
      setErr("");
    } catch (e) {
      if (n === seq.current) setErr(errorText(e));
    } finally {
      if (n === seq.current) setBusy(false);
    }
  };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { setReads(null); load(); }, [a, f]);

  const types = useMemo(() => filterOptions(VEHICLE_TYPES, (reads ?? []).map((r) => r.vehicle_type)), [reads]);
  const colors = useMemo(() => filterOptions(VEHICLE_COLORS, (reads ?? []).map((r) => r.vehicle_color)), [reads]);
  const cams = status?.cameras ?? [];
  const filtered = f.camera || f.type || f.color || f.direction || f.list || f.plates || f.min_conf || f.noise || f.period !== NO_FILTERS.period;

  return (
    <section className="plates-reads">
      <form className="search-bar row" onSubmit={(e) => { e.preventDefault(); const q = typed.trim(); setF((x) => (x.q === q ? x : { ...x, q })); }}>
        <input type="search" value={typed} onChange={(e) => setTyped(e.target.value)} placeholder="Plate or part of one, e.g. 1691 or 1691AJN"
          aria-label="Search plates" autoCapitalize="characters" autoComplete="off" spellCheck={false} />
        <button type="submit"><Icon name="find" size={16} /> Search</button>
      </form>
      {near.length > 0 && (
        <div className="chips plates-near" role="list" aria-label="Plates one character off">
          {near.map((p) => (
            <button key={p} type="button" role="listitem" className="chip" title="One character off the search: search this plate"
              onClick={() => { setTyped(p); set({ q: p }); }}>{nearChip(p)}</button>
          ))}
        </div>
      )}
      <div className="toolbar plates-filters">
        <select value={f.period} onChange={(e) => set({ period: e.target.value as Period })} aria-label="Period">
          {PERIODS.map(([k, l]) => <option key={k} value={k}>{l}</option>)}
        </select>
        {cams.length > 1 && (
          <select value={f.camera} onChange={(e) => set({ camera: e.target.value })} aria-label="Camera">
            <option value="">All cameras</option>
            {cams.map((c) => <option key={c.camera_id} value={c.camera_id}>{c.name}</option>)}
          </select>
        )}
        <select value={f.type} onChange={(e) => set({ type: e.target.value })} aria-label="Vehicle type">
          <option value="">Any type</option>
          {types.map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
        <select value={f.color} onChange={(e) => set({ color: e.target.value })} aria-label="Colour">
          <option value="">Any colour</option>
          {colors.map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
        <select value={f.direction} onChange={(e) => set({ direction: e.target.value as Filters["direction"] })} aria-label="Direction (reported)"
          title="The direction the camera reports: often wrong">
          <option value="">Either direction</option>
          <option value="away">Going away (reported)</option>
          <option value="approach">Approaching (reported)</option>
        </select>
        <select value={f.list} onChange={(e) => set({ list: e.target.value as Filters["list"] })} aria-label="List">
          <option value="">Any list or none</option>
          <option value="any">On a list</option>
          {PLATE_LISTS.map((l) => <option key={l} value={l}>{listName(l)}</option>)}
        </select>
        <select value={f.min_conf} onChange={(e) => set({ min_conf: Number(e.target.value) })} aria-label="Plate confidence">
          <option value={0}>Any confidence</option>
          <option value={0.6}>60 % and up</option>
          <option value={0.8}>80 % and up</option>
        </select>
        <label className="row small" title="Only vehicles whose plate the camera read (a search shows plates anyway)">
          <input type="checkbox" checked={f.plates} onChange={(e) => set({ plates: e.target.checked })} /> Plates only
        </label>
        <label className="row small" title="Reads with no vehicle and no or a weak plate: something the camera mistook for a plate, like a sign at night. Hidden unless ticked (a plate search shows them anyway)">
          <input type="checkbox" checked={f.noise} onChange={(e) => set({ noise: e.target.checked })} /> Show reads with no vehicle
        </label>
        {filtered ? <button type="button" className="linkish" onClick={() => setF({ ...NO_FILTERS, q: f.q })}>Clear filters</button> : null}
      </div>
      {err && <p className="error small">{err}</p>}
      {!reads && busy && <p className="muted small">Loading reads…</p>}
      {reads && reads.length === 0 && (
        <div className="empty">{f.q ? `No plate like ${formatPlate(f.q) || f.q} in this period.` : "No reads in this period."}</div>
      )}
      <div className="plate-reads">
        {reads?.map((r) => <ReadRow key={r.id} r={r} a={a} camera={name(r.camera_id)} onOpen={() => setOpen(r.id)} onEvent={onEvent} />)}
      </div>
      {reads && more && (
        <div className="center"><button className="ghost" disabled={busy} onClick={() => load(reads[reads.length - 1]?.id)}>{busy ? "Loading…" : "Load more"}</button></div>
      )}
      {open !== null && <ReadPanel a={a} id={open} name={name} onClose={() => setOpen(null)} onEvent={onEvent}
        onTimeline={(camera, ts) => { setOpen(null); onTimeline(camera, ts); }} />}
    </section>
  );
}

/** The read's list badge (deny red, watch amber, allow green; "possible" for a near match). */
function ListBadge({ r }: { r: Pick<Read, "list" | "list_match"> }) {
  const b = listBadge(r);
  return b ? <span className={`badge ${b.className}`} title={b.title}>{b.text}</span> : null;
}

function ReadRow({ r, a, camera, onOpen, onEvent }: { r: Read; a: SiteApi; camera: string; onOpen: () => void; onEvent: (id: number) => void }) {
  const band = confBand(r.confidence);
  const hints = [stateText(r.region), directionText(r.direction)].filter(Boolean).join(" · ");
  return (
    <div className={`plate-read${r.list ? ` on-${r.list}` : ""}`}>
      <button className="plate-read-main" onClick={onOpen} title="Open this read">
        <span className="plate-read-thumbs">
          <VehicleThumb r={r} a={a} />
          {r.images.includes("plate") && <img className="plate-crop" src={a.trafficImage(r.id, "plate")} alt="" loading="lazy" />}
        </span>
        <span className="plate-read-body">
          <span className="plate-read-head">
            {!r.plate && r.otso_plate
              ? <><strong className="plate-text">{formatPlate(r.otso_plate)}</strong><span className="badge" title="Read by Otso when a person asked; the camera read no plate">Otso</span></>
              : <strong className={`plate-text${r.plate ? "" : " muted"}`}>{plateText(r)}</strong>}
            {band && <span className={`badge conf-${band}`} title="The camera's confidence in the plate">{confText(r.confidence)}</span>}
            <ListBadge r={r} />
          </span>
          <span className="small">{vehicleText(r) || <span className="muted">vehicle</span>}</span>
          {hints && <span className="muted small">{hints}</span>}
          <span className="muted small" title={new Date(r.ts * 1000).toLocaleString()}>{fmtWhen(r.ts)} · {camera}</span>
        </span>
      </button>
      {r.event_id != null && (
        <button className="ghost small plate-read-event" onClick={() => onEvent(r.event_id!)} title="Open the event this read is on">
          <Icon name="play" size={14} /> Event
        </button>
      )}
    </div>
  );
}

/** One read: the larger picture, its details, the other reads of the vehicle's pass, quick list adds. */
/** The vehicle's picture in a row: the camera's, or (it sent none) the one the server cuts from the recording on
 *  first view; the vehicle type when there is neither. */
function VehicleThumb({ r, a }: { r: Read; a: SiteApi }) {
  const [failed, setFailed] = useState(false);
  if (failed) return <span className="thumb-empty vehicle-crop">{r.vehicle_type ?? "vehicle"}</span>;
  return <img className="vehicle-crop" src={a.trafficImage(r.id, "vehicle")} alt="" loading="lazy" onError={() => setFailed(true)}
    title={r.vehicle_source === "camera" ? undefined : "From the recording: the camera sent no picture of the vehicle"} />;
}

function ReadPanel({ a, id: initial, name, onClose, onEvent, onTimeline }: {
  a: SiteApi; id: number; name: (id: string) => string; onClose: () => void; onEvent: (id: number) => void;
  onTimeline: (camera: string, ts: number) => void;
}) {
  const [noPicture, setNoPicture] = useState(false);
  const [id, setId] = useState(initial);
  const [r, setR] = useState<ReadDetail | null>(null);
  const [err, setErr] = useState("");
  const [full, setFull] = useState(false);
  const [reading, setReading] = useState(false);
  useEffect(() => {
    let alive = true;
    setErr("");
    a.trafficRead(id).then((x) => { if (alive) { setR(x); setFull(false); setNoPicture(false); } }).catch((e) => { if (alive) setErr(errorText(e)); });
    return () => { alive = false; };
  }, [a, id]);
  useEffect(() => {
    // Escape closes what is on top: a prompt or the event viewer opened from here first, this panel after
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape" && document.querySelectorAll(".modal-backdrop").length <= 1) onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  const readPlate = async () => {
    if (!r) return;
    setReading(true);
    try {
      const res = await a.trafficReadPlate(r.id);
      setR({ ...res.read, pass: r.pass });
      if (res.result === "read" && res.plate) {
        toast.success(`Otso read ${formatPlate(res.plate, res.region)}`);
        if (res.list && res.list.list !== "allow") toast.error(`${formatPlate(res.plate)} is ${res.list.match === "near" ? "one character off a plate " : ""}on the ${res.list.list} list${res.list.note ? ` (${res.list.note})` : ""}`);
      } else toast.error("Otso couldn't read this plate either");
    } catch (e) { toast.error(plateError(e)); }
    setReading(false);
  };
  const add = async (list: PlateList) => {
    const plate = r && knownPlate(r);
    if (!plate) return;
    const note = await promptDialog(`Add ${formatPlate(plate)} to the ${list} list?`, {
      message: list === "allow" ? "Its reads are marked; no alarm." : `A read of it raises a ${list === "deny" ? "high" : "medium"}-priority alarm.`,
      label: "Note (who, why)", confirmLabel: "Add",
    });
    if (note === null) return;
    try {
      await a.addPlateEntry({ plate, list, note });
      toast.success(`${formatPlate(plate)} added to the ${list} list`);
    } catch (e) { toast.error(listError(e, errorText)); }
  };
  // the vehicle picture is asked for even when the camera sent none: the server cuts it from the recording then
  const shown = r && (full && r.images.includes("full") ? "full" : !noPicture ? "vehicle" : r.images.includes("full") ? "full" : null);
  const fromRecording = !!r && shown === "vehicle" && r.vehicle_source !== "camera";
  const pass = (r?.pass ?? []).filter((x) => x.id !== r?.id);
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal plate-panel" role="dialog" aria-modal="true" aria-label="Plate read" onClick={(e) => e.stopPropagation()}>
        <header className="modal-head">
          <h2>{r ? plateText(r) : "Read"}{r && <span className="muted small"> · {name(r.camera_id)} · {fmtTime(r.ts)}</span>}</h2>
          <button className="ghost" onClick={onClose} aria-label="Close">✕</button>
        </header>
        {err && <p className="error small plate-panel-body">{err}</p>}
        {r && (
          <div className="plate-panel-body">
            <div className="plate-panel-picture">
              {shown ? <img src={a.trafficImage(r.id, shown)} alt={`${vehicleText(r) || "Vehicle"} ${r.plate ?? ""}`}
                  onError={() => { if (shown === "vehicle") setNoPicture(true); }} />
                : <div className="thumb-empty">No picture: nothing recorded at this moment</div>}
              {fromRecording && <p className="muted small">From the recording: the camera sent no picture of the vehicle. Check the moment in the Timeline.</p>}
              <div className="row small">
                {r.images.includes("plate") && <img className="plate-crop big" src={a.trafficImage(r.id, "plate")} alt={`Plate ${r.plate ?? ""}`} />}
                {r.images.includes("full") && r.images.includes("vehicle") && (
                  <button className="ghost small" onClick={() => setFull(!full)}>{full ? "Vehicle" : "Full frame"}</button>
                )}
                {r.images.includes("violation") && <a className="small" href={a.trafficImage(r.id, "violation")} target="_blank" rel="noopener noreferrer">Violation picture ↗</a>}
              </div>
            </div>
            <div className="plate-panel-facts">
              <div className="plate-read-head">
                <strong className={`plate-text big${r.plate ? "" : " muted"}`}>{plateText(r)}</strong>
                {r.confidence != null && <span className={`badge conf-${confBand(r.confidence)}`}>{confText(r.confidence)}</span>}
                <ListBadge r={r} />
              </div>
              <dl className="plate-facts">
                {r.plate_raw && r.plate_raw !== r.plate && <><dt>As read</dt><dd>{r.plate_raw}</dd></>}
                {r.otso && <><dt>Otso</dt><dd>{otsoText(r.otso)} <span className="muted small">(read on request, {new Date(r.otso.at * 1000).toLocaleTimeString()})</span></dd></>}
                {r.region && <><dt>State</dt><dd>{r.region} <span className="muted small">(reported by the camera: a hint)</span></dd></>}
                <dt>Vehicle</dt><dd>{vehicleText(r) || <span className="muted">not given</span>}</dd>
                {r.direction && <><dt>Direction</dt><dd>{directionText(r.direction)}</dd></>}
                {r.speed != null && <><dt>Speed</dt><dd>{r.speed}</dd></>}
                {(r.plate_type || r.plate_color) && <><dt>Plate</dt><dd>{[r.plate_color, r.plate_type].filter(Boolean).join(" · ")}</dd></>}
                {r.detection_region && <><dt>Region</dt><dd>{r.detection_region}</dd></>}
                <dt>Time</dt><dd>{fmtTime(r.ts)}{Math.abs(r.received_at - r.ts) > 120 && <span className="muted small"> (received {fmtTime(r.received_at)})</span>}</dd>
                <dt>Camera</dt><dd>{name(r.camera_id)}</dd>
              </dl>
              <div className="row">
                {r.event_id != null && <button onClick={() => onEvent(r.event_id!)}><Icon name="play" size={14} /> Open event</button>}
                <button className="ghost" onClick={() => onTimeline(r.camera_id, r.ts)} title="The recording at this moment">
                  <Icon name="timeline" size={14} /> Timeline
                </button>
                {r.images.includes("vehicle") && (
                  <button className="ghost" disabled={reading} onClick={readPlate}
                    title="Otso looks at the camera's picture of this vehicle and reads the plate only if it is clearly legible">
                    <Icon name="sparkle" size={14} /> {reading ? "Reading…" : r.otso ? "Read again" : r.plate ? "Check with Otso" : "Read plate"}
                  </button>
                )}
                {knownPlate(r) && <span className="row small plates-add">
                  <span className="muted">Add to</span>
                  {PLATE_LISTS.map((l) => <button key={l} className={`ghost small list-${l}`} onClick={() => add(l)}>{listName(l).replace(/ list$/, "")}</button>)}
                </span>}
              </div>
              {pass.length > 0 && <>
                <h3 className="spaced">This pass ({pass.length + 1} reads)</h3>
                <ul className="plain plate-pass">
                  {r.pass!.map((x) => (
                    <li key={x.id} className={x.id === r.id ? "current" : ""}>
                      <button className="linkish" disabled={x.id === r.id} onClick={() => setId(x.id)}>
                        {x.images.includes("plate") && <img className="plate-crop" src={a.trafficImage(x.id, "plate")} alt="" loading="lazy" />}
                        <span className="plate-text">{plateText(x)}</span>
                      </button>
                      <span className="muted small">{confText(x.confidence)}{x.best ? " · best" : ""} · {new Date(x.ts * 1000).toLocaleTimeString()}</span>
                    </li>
                  ))}
                </ul>
              </>}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- lists

type ListDraft = { plate: string; list: PlateList; note: string; expires: string };
const NO_DRAFT: ListDraft = { plate: "", list: "deny", note: "", expires: "" };

function ListsSection({ a, name }: { a: SiteApi; name: (id: string) => string }) {
  const [entries, setEntries] = useState<ListEntry[] | null>(null);
  const [err, setErr] = useState("");
  const [d, setD] = useState<ListDraft>(NO_DRAFT);
  const [busy, setBusy] = useState(false);
  const [edit, setEdit] = useState<{ id: number; note: string; expires: string } | null>(null);
  const load = () => a.plateLists().then((x) => { setEntries(x); setErr(""); }).catch((e) => setErr(errorText(e)));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { load(); }, [a]);

  const problem = d.plate ? plateProblem(d.plate) : null;
  const dup = entries && d.plate && !problem && onList(entries, d.plate, d.list);
  const add = async () => {
    if (!d.plate || problem || dup) return;
    setBusy(true);
    try {
      await a.addPlateEntry({ plate: d.plate, list: d.list, note: d.note.trim(), expires_at: expiryFromDate(d.expires) });
      toast.success(`${formatPlate(d.plate)} added to the ${d.list} list`);
      setD({ ...NO_DRAFT, list: d.list });
      load();
    } catch (e) { toast.error(listError(e, errorText)); }
    setBusy(false);
  };
  const save = async () => {
    if (!edit) return;
    const exp = expiryFromDate(edit.expires);
    try {
      await a.editPlateEntry(edit.id, exp ? { note: edit.note.trim(), expires_at: exp } : { note: edit.note.trim(), clear_expiry: true });
      setEdit(null);
      load();
    } catch (e) { toast.error(listError(e, errorText)); }
  };
  const remove = async (e: ListEntry) => {
    if (!await confirmDialog(`Take ${formatPlate(e.plate)} off the ${e.list} list?`, {
      message: e.list === "allow" ? "Its reads are no longer marked." : "Its reads no longer raise an alarm.", confirmLabel: "Remove", danger: true,
    })) return;
    try {
      await a.deletePlateEntry(e.id);
      toast.success(`${formatPlate(e.plate)} removed from the ${e.list} list`);
      load();
    } catch (x) { toast.error(listError(x, errorText)); }
  };

  return (
    <section className="plates-lists">
      <p className="muted small">
        A plate on the deny list raises a high-priority alarm when a camera reads it, the watch list a medium one; the allow list only marks
        its reads. A plate one character off is a possible match (the alarm says so). Changes need the operator role.
      </p>
      <form className="row plates-add-form" onSubmit={(e) => { e.preventDefault(); add(); }}>
        <label className="field"><span>Plate</span>
          <input value={d.plate} onChange={(e) => setD({ ...d, plate: e.target.value })} placeholder="e.g. 1691AJN" autoCapitalize="characters" autoComplete="off" spellCheck={false} />
        </label>
        <label className="field"><span>List</span>
          <select value={d.list} onChange={(e) => setD({ ...d, list: e.target.value as PlateList })}>
            {PLATE_LISTS.map((l) => <option key={l} value={l}>{listName(l)}</option>)}
          </select>
        </label>
        <label className="field grow"><span>Note</span>
          <input value={d.note} maxLength={200} onChange={(e) => setD({ ...d, note: e.target.value })} placeholder="who, why (e.g. trespass notice 10/12)" />
        </label>
        <label className="field"><span>Expires (optional)</span>
          <input type="date" value={d.expires} onChange={(e) => setD({ ...d, expires: e.target.value })} />
        </label>
        <button type="submit" disabled={busy || !d.plate || !!problem || !!dup}>{busy ? "Adding…" : "Add"}</button>
      </form>
      {problem && <p className="error small">{problem}</p>}
      {dup && <p className="muted small">{formatPlate(d.plate)} is already on the {d.list} list.</p>}
      {err && <p className="error small">{err}</p>}
      {entries && entries.length === 0 && <div className="empty">No plates on any list yet.</div>}
      {entries && entries.length > 0 && (
        <table className="table plates-table">
          <thead><tr><th>Plate</th><th>List</th><th>Note</th><th>Expires</th><th>Last seen</th><th /></tr></thead>
          <tbody>
            {entries.map((e) => edit?.id === e.id ? (
              <tr key={e.id}>
                <td><strong className="plate-text">{formatPlate(e.plate)}</strong></td>
                <td><span className={`badge list-badge ${e.list}`}>{listName(e.list)}</span></td>
                <td><input value={edit.note} maxLength={200} autoFocus aria-label="Note" onChange={(x) => setEdit({ ...edit, note: x.target.value })} /></td>
                <td><input type="date" value={edit.expires} aria-label="Expires" onChange={(x) => setEdit({ ...edit, expires: x.target.value })} /></td>
                <td />
                <td className="row">
                  <button className="small" onClick={save}>Save</button>
                  <button className="ghost small" onClick={() => setEdit(null)}>Cancel</button>
                </td>
              </tr>
            ) : (
              <tr key={e.id} className={e.expired ? "muted" : ""}>
                <td><strong className="plate-text">{formatPlate(e.plate)}</strong></td>
                <td><span className={`badge list-badge ${e.list}`}>{listName(e.list)}</span></td>
                <td>{e.note || <span className="muted small">—</span>}{e.by && <div className="muted small">added by {e.by} · {fmtWhen(e.created_at)}</div>}</td>
                <td className="small">{expiryText(e)}</td>
                <td className="small">{e.last_seen ? <span title={fmtTime(e.last_seen)}>{timeAgo(e.last_seen)}{e.last_camera ? ` · ${name(e.last_camera)}` : ""}</span> : <span className="muted">not seen</span>}</td>
                <td className="row">
                  <button className="ghost small" onClick={() => setEdit({ id: e.id, note: e.note, expires: dateFromExpiry(e.expires_at) })}>Edit</button>
                  <button className="ghost small" onClick={() => remove(e)}>Remove</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

// ---------------------------------------------------------------- traffic

function TrafficSection({ a, status }: { a: SiteApi; status: TrafficStatus | null }) {
  const [camera, setCamera] = useState("");
  const cams = status?.cameras ?? [];
  const now = Date.now() / 1000;
  const weekStart = useMemo(() => { const d = new Date(); d.setHours(0, 0, 0, 0); d.setDate(d.getDate() - 6); return d.getTime() / 1000; }, []);
  return (
    <section className="plates-traffic">
      {cams.length > 0 && (
        <table className="table plates-table">
          <thead><tr><th>Camera</th><th>Push</th><th>Today</th></tr></thead>
          <tbody>
            {cams.map((c) => {
              const silent = status && isSilent(c, status.silent_after_s, now);
              return (
                <tr key={c.camera_id}>
                  <td><strong>{c.name}</strong></td>
                  <td className="small">
                    <span className={`dot ${!c.enabled ? "idle" : silent ? "bad" : "ok"}`} /> {pushText(c, now)}
                    {silent && <div className="warn-text small">⚠ Nothing from the camera for over {Math.round((status?.silent_after_s ?? 900) / 60)} min: check its network and its push.</div>}
                  </td>
                  <td className="small">{c.reads_today.toLocaleString()} vehicles · {c.plates_today.toLocaleString()} plates</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      {cams.length > 1 && (
        <div className="toolbar">
          <select value={camera} onChange={(e) => setCamera(e.target.value)} aria-label="Camera">
            <option value="">All cameras</option>
            {cams.map((c) => <option key={c.camera_id} value={c.camera_id}>{c.name}</option>)}
          </select>
        </div>
      )}
      <CountsChart a={a} camera={camera} title="Last 24 hours" since={now - 86400} bucket="hour" />
      <CountsChart a={a} camera={camera} title="Last 7 days" since={weekStart} bucket="day" />
      <p className="muted small">One vehicle is one pass (the camera may read a vehicle more than once). Counts come from the reads themselves.</p>
    </section>
  );
}

function CountsChart({ a, camera, title, since, bucket }: { a: SiteApi; camera: string; title: string; since: number; bucket: "hour" | "day" }) {
  const [bars, setBars] = useState<Bar[] | null>(null);
  const [err, setErr] = useState("");
  // the window is fixed when the chart mounts (or its camera changes): re-reading every render would move it each second
  const start = useRef(since);
  useEffect(() => {
    let alive = true;
    a.trafficCounts({ camera: camera || undefined, since: start.current, bucket })
      .then((c) => { if (alive) { setBars(countBars(c)); setErr(""); } }).catch((e) => { if (alive) setErr(errorText(e)); });
    return () => { alive = false; };
  }, [a, camera, bucket]);
  const max = Math.max(1, ...(bars ?? []).map((b) => b.total));
  const every = bars && bars.length > 12 ? Math.ceil(bars.length / 8) : 1;
  return (
    <div className="device-card counts-card">
      <div className="row"><h3>{title}</h3><span className="spacer" />{bars && <span className="muted small">{countsSummary(bars)}</span>}</div>
      {err && <p className="error small">{err}</p>}
      {bars && (
        <div className="count-bars" role="img" aria-label={`${title}: ${countsSummary(bars)}`}>
          {bars.map((b, i) => (
            <div key={b.t} className="count-col" title={`${b.label}: ${b.total} vehicle${b.total === 1 ? "" : "s"}, ${b.plates} with a plate`}>
              <div className="count-bar" style={{ height: `${(b.total / max) * 100}%` }}>
                <div className="count-plates" style={{ height: b.total ? `${(b.plates / b.total) * 100}%` : 0 }} />
              </div>
              <span className="count-label">{i % every === 0 ? b.label : ""}</span>
            </div>
          ))}
        </div>
      )}
      <div className="row small muted count-legend">
        <span><span className="count-swatch all" /> all vehicles</span>
        <span><span className="count-swatch plates" /> with a plate</span>
      </div>
    </div>
  );
}
