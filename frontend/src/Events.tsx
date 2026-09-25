import { useEffect, useRef, useState } from "react";
import { api, fmtDuration, fmtTime, media, UNUSUAL_MIN, type Camera, type NvrEvent } from "./api";
import { ConfidenceSlider, loadNumber, saveNumber } from "./ConfidenceSlider";
import { EventDetail } from "./EventDetail";
import { IdentitiesView } from "./Identities";

const STATUS_LABEL: Record<string, string> = {
  open: "Tracking",
  pending: "Verifying",
  verified: "Verified",
  rejected: "Rejected",
  error: "Error",
  masked: "Masked",
};

export function StatusBadge({ e }: { e: Pick<NvrEvent, "status" | "threat"> }) {
  if (e.status === "verified" && e.threat) return <span className={`badge threat-${e.threat}`}>{e.threat === "none" ? "Routine" : `${e.threat} threat`}</span>;
  return <span className={`badge status-${e.status}`}>{STATUS_LABEL[e.status]}</span>;
}

/** Qwen synopses are generated for people only; other labels show the YOLO verification result. */
export function placeholder(e: NvrEvent): string {
  if (e.status === "verified") {
    if (e.camera_class !== "person") return `${e.yolo_class ?? e.camera_class} confirmed by YOLO in ${e.yolo_hits ?? 0} frames.`;
    return e.error ?? "Writing synopsis…";
  }
  if (e.status === "rejected") return "YOLO did not confirm the camera detection.";
  return e.error ?? "Waiting for verification…";
}

export function EventCard({ e, cameraName, onOpen }: { e: NvrEvent; cameraName: string; onOpen: () => void }) {
  return (
    <button className={`event-card ${e.status}`} onClick={onOpen}>
      <div className="thumb">
        {e.snapshot ? <img src={media(e, "snapshot.jpg")} loading="lazy" alt="" /> : <div className="thumb-empty">{e.camera_class}</div>}
        <span className={`label-chip ${e.camera_class}`}>{e.yolo_class ?? e.camera_class}</span>
      </div>
      <div className="event-body">
        <div className="event-meta">
          <span className="cam">{cameraName}</span>
          <span className="muted">{fmtTime(e.start_ts)} · {fmtDuration(e)}</span>
        </div>
        <p className={e.synopsis ? "synopsis" : "synopsis muted"}>{e.synopsis ?? placeholder(e)}</p>
        <div className="event-foot">
          <StatusBadge e={e} />
          {e.feedback?.verdict === "false_alarm" && <span className="badge status-error">False alarm</span>}
          {e.corrected_at ? <span className="badge" title="Synopsis corrected by an operator">Corrected</span> : null}
          {e.locked ? <span className="badge lock-badge" title="Footage locked: kept regardless of retention">🔒</span> : null}
          {(e.anomaly ?? 0) >= UNUSUAL_MIN ? <span className="badge unusual-badge" title={`Unusual for this camera:\n${(e.anomaly_json?.reasons ?? []).join("\n")}`}>⚠ Unusual</span> : null}
          {e.journey_cameras && e.journey_cameras > 1 ? <span className="badge journey-badge" title="Same person seen on other cameras">🔗 {e.journey_cameras} cams</span> : null}
          {e.yolo_conf != null && <span className="muted small">YOLO {(e.yolo_conf * 100).toFixed(0)}% · cam {((e.camera_conf ?? 0) * 100).toFixed(0)}%</span>}
        </div>
      </div>
    </button>
  );
}

export function EventsView({ cameras, live }: { cameras: Camera[]; live: NvrEvent | null }) {
  const [events, setEvents] = useState<NvrEvent[]>([]);
  const [filter, setFilter] = useState({ status: "verified", camera: "", label: "", min_yolo: loadNumber("minYolo.events"), until: undefined as number | undefined });
  const [jump, setJump] = useState("");
  const sentinel = useRef<HTMLDivElement>(null);
  const loadingMore = useRef(false);
  const [open, setOpen] = useState<number | null>(null);
  const [more, setMore] = useState(true);
  // "grouped": repeated sightings of the same person/vehicle become one row (Identities.tsx)
  const [mode, setMode] = useState<"sightings" | "grouped">(() => (loadNumber("eventsGrouped", 0) === 1 ? "grouped" : "sightings"));
  const name = (id: string) => cameras.find((c) => c.id === id)?.name ?? id;

  useEffect(() => {
    api.events({ ...filter, limit: 60 }).then((r) => {
      setEvents(r);
      setMore(r.length === 60);
    });
  }, [filter]);

  useEffect(() => {
    if (!live) return;
    const statusOk = !filter.status || filter.status.split(",").includes(live.status);
    const camOk = !filter.camera || filter.camera === live.camera_id;
    const labelOk = !filter.label || filter.label === live.camera_class;
    const confOk = !filter.min_yolo || live.status === "open" || live.status === "pending" || (live.yolo_conf ?? 0) >= filter.min_yolo;
    setEvents((prev) => {
      const rest = prev.filter((x) => x.id !== live.id);
      return statusOk && camOk && labelOk && confOk ?[live, ...rest].sort((a, b) => b.id - a.id) : rest;
    });
  }, [live, filter]);

  const loadMore = async () => {
    if (loadingMore.current || !more || !events.length) return;
    loadingMore.current = true;
    try {
      const r = await api.events({ ...filter, limit: 60, before_id: events[events.length - 1]?.id });
      setEvents((p) => [...p, ...r]);
      setMore(r.length === 60);
    } finally { loadingMore.current = false; }
  };
  // older events load as you scroll to the bottom
  useEffect(() => {
    const el = sentinel.current;
    if (!el || mode !== "sightings") return;
    const io = new IntersectionObserver((entries) => { if (entries[0].isIntersecting) loadMore(); }, { rootMargin: "600px" });
    io.observe(el);
    return () => io.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [events, more, mode]);

  return (
    <div className="view">
      <div className="toolbar">
        <div className="segmented">
          <button className={mode === "sightings" ? "active" : ""} onClick={() => { setMode("sightings"); saveNumber("eventsGrouped", 0); }}>Sightings</button>
          <button className={mode === "grouped" ? "active" : ""} onClick={() => { setMode("grouped"); saveNumber("eventsGrouped", 1); }} title="Group repeated sightings of the same person or vehicle">People &amp; vehicles</button>
        </div>
        {mode === "sightings" && <div className="segmented">
          {[
            ["verified", "Verified"],
            ["open,pending", "In progress"],
            ["rejected", "Rejected"],
            ["", "All"],
          ].map(([v, l]) => (
            <button key={l} className={filter.status === v ? "active" : ""} onClick={() => setFilter({ ...filter, status: v })}>
              {l}
            </button>
          ))}
        </div>}
        {mode === "sightings" && <select value={filter.camera} onChange={(e) => setFilter({ ...filter, camera: e.target.value })}>
          <option value="">All cameras</option>
          {cameras.map((c) => (
            <option key={c.id} value={c.id}>{c.name}</option>
          ))}
        </select>}
        {mode === "sightings" && <select value={filter.label} onChange={(e) => setFilter({ ...filter, label: e.target.value })}>
          <option value="">People &amp; vehicles</option>
          <option value="person">People</option>
          <option value="vehicle">Vehicles</option>
        </select>}
        {mode === "sightings" && <ConfidenceSlider value={filter.min_yolo} onChange={(v) => {
          saveNumber("minYolo.events", v);
          setFilter({ ...filter, min_yolo: v });
        }} />}
        {mode === "sightings" && <label className="row small" title="Show events from the end of this day backwards">
          <input type="date" value={jump} max={new Date().toISOString().slice(0, 10)} onChange={(e) => {
            setJump(e.target.value);
            const d = e.target.value ? new Date(e.target.value + "T23:59:59") : null;
            setFilter({ ...filter, until: d && Number.isFinite(d.getTime()) ? d.getTime() / 1000 : undefined });
          }} />
          {jump && <button className="linkish small" onClick={() => { setJump(""); setFilter({ ...filter, until: undefined }); }}>now</button>}
        </label>}
      </div>
      {mode === "grouped" ? <IdentitiesView cameras={cameras} /> : events.length === 0 ? (
        <div className="empty">No events match these filters yet.</div>
      ) : (
        <div className="event-grid">
          {events.map((e) => (
            <EventCard key={e.id} e={e} cameraName={name(e.camera_id)} onOpen={() => setOpen(e.id)} />
          ))}
        </div>
      )}
      {mode === "sightings" && <div ref={sentinel} className="center muted small">{more && events.length > 0 ? "Loading older events…" : events.length > 0 ? "That's everything." : ""}</div>}
      {open !== null && <EventDetail id={open} cameraName={name} onClose={() => setOpen(null)} />}
    </div>
  );
}
