import { api, fmtDuration, fmtWhen, ppeBadge, UNUSUAL_MIN, type NvrEvent, type SiteApi } from "./api";

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

/** Card chip: the combined priority (threat or unusualness, operator wins), hidden when there is none;
 * events the verifier hasn't finished (or rejected) still say so. */
export function CardStatus({ e }: { e: Pick<NvrEvent, "status" | "priority"> }) {
  if (e.status !== "verified") return <span className={`badge status-${e.status}`}>{STATUS_LABEL[e.status]}</span>;
  if (!e.priority || e.priority === "none") return null;
  return <span className={`badge threat-${e.priority}`} title="Priority: the higher of Qwen's threat level and how unusual this is for the camera">{e.priority}</span>;
}

const clip = (t: string, n = 40) => (t.length > n ? `${t.slice(0, n - 1).trimEnd()}…` : t);

/** Why the verifier rejected an event, when it recorded one (detections.rejected, e.g. backend/nvr/parked.py):
 * "parked vehicle, motion elsewhere" -> "Parked vehicle · motion elsewhere". */
export function rejectedReason(e: Pick<NvrEvent, "status" | "detections">): string | null {
  const r = e.status === "rejected" ? e.detections?.rejected : null;
  return r ? (r.charAt(0).toUpperCase() + r.slice(1)).replace(/, /g, " · ") : null;
}

/** What to show in place of a synopsis: Qwen's progress, or the YOLO result for labels Qwen skips. */
export function placeholder(e: NvrEvent): string {
  if (e.status === "verified") {
    if (e.synopsis_pending) return "Qwen is writing a synopsis…";
    if (e.camera_class !== "person") return e.error ?? `${e.yolo_class ?? e.camera_class} confirmed by YOLO in ${e.yolo_hits ?? 0} frames.`;
    return e.error ?? "Writing synopsis…";
  }
  if (e.status === "rejected") return rejectedReason(e) ?? "YOLO did not confirm the camera detection.";
  return e.error ?? "Waiting for verification…";
}

export function EventCard({ e, cameraName, onOpen, site = api, siteName, focused }: {
  e: NvrEvent; cameraName: string; onOpen: () => void;
  /** the site the event belongs to (hub dashboard: another site than the page's); default: this site */
  site?: SiteApi; siteName?: string;
  /** keyboard focus in Find (j/k) */
  focused?: boolean;
}) {
  const unusual = (e.anomaly ?? 0) >= UNUSUAL_MIN;
  const reasons = e.anomaly_json?.reasons ?? [];
  return (
    <button className={`event-card ${e.status}${focused ? " focused" : ""}`} onClick={onOpen} data-event-id={e.id}>
      <div className="thumb">
        {e.snapshot ? <img src={site.media(e, "snapshot.jpg")} loading="lazy" alt="" /> : <div className="thumb-empty">{e.camera_class}</div>}
        <span className={`label-chip ${e.camera_class}`}>{e.yolo_class ?? e.camera_class}</span>
      </div>
      <div className="event-body">
        <div className="event-meta">
          <span className="cam">{siteName ? `${siteName} · ` : ""}{cameraName}</span>
          <span className="muted" title={new Date(e.start_ts * 1000).toLocaleString()}>{fmtWhen(e.start_ts)} · {fmtDuration(e)}</span>
        </div>
        <p className={e.synopsis ? "synopsis" : "synopsis muted"}>{e.synopsis ?? placeholder(e)}</p>
        {/* fixed order, every slot optional, so footers line up: priority, rule, unusual, watched, places,
            journey, camera away, lock / corrected / false alarm, then the confidence line on the right */}
        <div className="event-foot">
          <CardStatus e={e} />
          {e.policy ? <span className={`badge trunc threat-${e.policy.priority === "medium" ? "medium" : "high"}`} title={e.policy.text}>{e.policy.kind === "ppe" ? `🦺 ${ppeBadge(e.policy)}` : `🚫 ${clip(e.policy.text || "Site rule")}`}</span> : null}
          {unusual ? <span className="badge trunc unusual-badge" title={`Unusual for this camera:\n${reasons.join("\n")}`}>⚠ Unusual{reasons[0] ? ` · ${reasons[0]}` : ""}</span> : null}
          {e.watched ? <span className="badge threat-medium" title="On the watch list">👁 {e.watched}</span> : null}
          {e.areas?.length ? <span className="badge trunc area-badge" title={`Went to: ${e.areas.map((x) => x.name).join(" → ")}`}>📍 {e.areas.map((x) => x.name).join(" → ")}</span> : null}
          {e.journey_cameras && e.journey_cameras > 1 ? <span className="badge journey-badge" title="Same person seen on other cameras">🔗 {e.journey_cameras} cams</span> : null}
          {e.ptz_preset ? <span className="badge ptz-badge" title="The camera was turned away from its home view: zones, places and the learned baseline did not apply">↗ {e.ptz_preset === "away" ? "camera away" : e.ptz_preset}</span> : null}
          {e.locked ? <span className="badge lock-badge" title="Footage locked: kept regardless of retention">🔒</span> : null}
          {e.corrected_at ? <span className="badge" title="Synopsis corrected by an operator">Corrected</span> : null}
          {e.feedback?.verdict === "false_alarm" && <span className="badge status-error">False alarm</span>}
          {e.yolo_conf != null && <span className="muted small conf">YOLO {(e.yolo_conf * 100).toFixed(0)}% · cam {((e.camera_conf ?? 0) * 100).toFixed(0)}%</span>}
        </div>
      </div>
    </button>
  );
}
