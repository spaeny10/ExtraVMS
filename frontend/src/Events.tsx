import { fmtDuration, fmtTime, media, UNUSUAL_MIN, type NvrEvent } from "./api";

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

/** What to show in place of a synopsis: Qwen's progress, or the YOLO result for labels Qwen skips. */
export function placeholder(e: NvrEvent): string {
  if (e.status === "verified") {
    if (e.synopsis_pending) return "Qwen is writing a synopsis…";
    if (e.camera_class !== "person") return e.error ?? `${e.yolo_class ?? e.camera_class} confirmed by YOLO in ${e.yolo_hits ?? 0} frames.`;
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
          {e.watched ? <span className="badge threat-medium" title="On the watch list">👁 {e.watched}</span> : null}
          {e.policy ? <span className={`badge threat-${e.policy.priority === "medium" ? "medium" : "high"}`} title={e.policy.text}>🚫 Site rule</span> : null}
          {e.ptz_preset ? <span className="badge ptz-badge" title="The camera was turned away from its home view: zones, places and the learned baseline did not apply">↗ {e.ptz_preset === "away" ? "camera away" : e.ptz_preset}</span> : null}
          {e.areas?.length ? <span className="badge area-badge" title={`Went to: ${e.areas.map((a) => a.name).join(" → ")}`}>📍 {e.areas.map((a) => a.name).join(" → ")}</span> : null}
          {(e.anomaly ?? 0) >= UNUSUAL_MIN ? <span className="badge unusual-badge" title={`Unusual for this camera:\n${(e.anomaly_json?.reasons ?? []).join("\n")}`}>⚠ Unusual</span> : null}
          {e.journey_cameras && e.journey_cameras > 1 ? <span className="badge journey-badge" title="Same person seen on other cameras">🔗 {e.journey_cameras} cams</span> : null}
          {e.yolo_conf != null && <span className="muted small">YOLO {(e.yolo_conf * 100).toFixed(0)}% · cam {((e.camera_conf ?? 0) * 100).toFixed(0)}%</span>}
        </div>
      </div>
    </button>
  );
}
