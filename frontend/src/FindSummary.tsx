import { useEffect, useState } from "react";
import { api, type EventSummary } from "./api";
import { eventQuery, filterWindow, type FindFilters } from "./findViews";

const KIND_LABEL: Record<string, string> = { ppe: "🦺 PPE", towing: "🚛 Towing", entry: "🚪 Entry" };
const localDay = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;

/** Days of the window, oldest first (at most 31), so quiet days show as empty bars. */
function windowDays(f: FindFilters, seen: string[]): string[] {
  const [since] = filterWindow(f);
  if (f.day) return [f.day];
  if (!since) return seen;
  const out: string[] = [];
  const d = new Date(since * 1000); d.setHours(12, 0, 0, 0);
  const today = localDay(new Date());
  while (out.length < 31) {
    const k = localDay(d);
    out.push(k);
    if (k >= today) break;
    d.setDate(d.getDate() + 1);
  }
  return out.slice(-31);
}

/** Compliance strip: counts for the active filters by rule kind, PPE zone, camera and day (backend db.summary).
 * Clicking a camera, zone or day narrows the filters; clicking the active one again widens them. */
export function FindSummary({ filters, cameraName, onCamera, onDay, onZone, zone, refresh }: {
  filters: FindFilters; cameraName: (id: string) => string;
  onCamera: (id: string) => void; onDay: (day: string) => void; onZone: (zone: string) => void;
  zone: string; refresh?: number;
}) {
  const [s, setS] = useState<EventSummary | null>(null);
  const key = JSON.stringify({ ...filters, zone });
  useEffect(() => {
    let live = true;
    api.eventsSummary({ ...eventQuery(filters), ppe_zone: zone || undefined }).then((r) => live && setS(r)).catch(() => live && setS(null));
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, refresh]);
  if (!s) return null;
  const counts = Object.fromEntries(s.by_day.map((d) => [d.day, d.n]));
  const days = windowDays(filters, s.by_day.map((d) => d.day));
  const max = Math.max(1, ...days.map((d) => counts[d] ?? 0));
  const dayLabel = (d: string) => new Date(d + "T12:00:00").toLocaleDateString(undefined, { month: "short", day: "numeric" });
  return (
    <section className="find-summary">
      <div className="fs-total"><b>{s.total}</b><span className="muted small">{s.total === 1 ? "event" : "events"}</span></div>
      <div className="fs-col">
        <span className="muted small">By rule</span>
        {s.by_kind.length === 0 && <span className="muted small">none broken</span>}
        {s.by_kind.map((k) => <span key={k.kind} className="fs-row"><span>{KIND_LABEL[k.kind] ?? k.kind}</span><b>{k.n}</b></span>)}
      </div>
      {(s.by_zone.length > 0 || zone) && (
        <div className="fs-col">
          <span className="muted small">PPE zone</span>
          {s.by_zone.map((z) => (
            <button key={z.zone} className={`fs-row linkish ${zone === z.zone ? "active" : ""}`} onClick={() => onZone(zone === z.zone ? "" : z.zone)}
              title={zone === z.zone ? "Show every zone" : `Only PPE violations in ${z.zone}`}><span>{z.zone}</span><b>{z.n}</b></button>
          ))}
        </div>
      )}
      <div className="fs-col">
        <span className="muted small">Camera</span>
        {s.by_camera.slice(0, 6).map((c) => (
          <button key={c.camera_id} className={`fs-row linkish ${filters.camera === c.camera_id ? "active" : ""}`}
            onClick={() => onCamera(filters.camera === c.camera_id ? "" : c.camera_id)}
            title={filters.camera === c.camera_id ? "Every camera" : `Only ${cameraName(c.camera_id)}`}><span>{cameraName(c.camera_id)}</span><b>{c.n}</b></button>
        ))}
      </div>
      <div className="fs-days" title="Events per day; click a day to see only that day">
        {days.map((d) => (
          <button key={d} className={`fs-day ${filters.day === d ? "active" : ""}`} onClick={() => onDay(filters.day === d ? "" : d)}
            title={`${dayLabel(d)}: ${counts[d] ?? 0}`}>
            <span className="fs-bar" style={{ height: `${Math.round(((counts[d] ?? 0) / max) * 100)}%` }} />
            {days.length <= 8 && <span className="fs-day-label">{dayLabel(d)}</span>}
          </button>
        ))}
      </div>
    </section>
  );
}
