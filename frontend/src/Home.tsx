import { useEffect, useState } from "react";
import { api, fmtTime, type Camera, type HomeData, type NvrEvent } from "./api";
import { BriefingCard } from "./Ask";
import { EventCard } from "./Events";
import { EventDetail } from "./EventDetail";
import { Skeleton, SkeletonGrid } from "./ui";

const SEEN_KEY = "homeSeenAt";

function loadSeen(): number {
  try {
    const v = Number(localStorage.getItem(SEEN_KEY));
    return Number.isFinite(v) && v > 0 ? v : Date.now() / 1000 - 86400;
  } catch {
    return Date.now() / 1000 - 86400;
  }
}

const ago = (ts: number) => {
  const s = Date.now() / 1000 - ts;
  return s < 90 ? "just now" : s < 5400 ? `${Math.round(s / 60)} min ago` : s < 172800 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} d ago`;
};

/** The daily entry point: what needs attention since you last looked, who's been on site today, and whether
 * everything is recording. */
export function HomeView({ cameras, onGo }: { cameras: Camera[]; onGo: (tab: "Live" | "Events" | "Settings") => void }) {
  const [seenAt] = useState(loadSeen);
  const [h, setH] = useState<HomeData | null>(null);
  const [err, setErr] = useState("");
  const [open, setOpen] = useState<number | null>(null);
  const name = (id: string) => cameras.find((c) => c.id === id)?.name ?? id;

  useEffect(() => {
    const load = () => api.home(seenAt).then((d) => { setH(d); setErr(""); }).catch((e) => setErr(String(e)));
    load();
    const t = setInterval(load, 30000);
    // what you've now seen becomes the new baseline for next time
    const mark = () => { try { localStorage.setItem(SEEN_KEY, String(Date.now() / 1000)); } catch { /* private mode */ } };
    window.addEventListener("beforeunload", mark);
    return () => { clearInterval(t); window.removeEventListener("beforeunload", mark); mark(); };
  }, [seenAt]);

  if (err && !h) return <div className="view error">{err}</div>;
  if (!h) return <div className="view home"><Skeleton lines={1} /><Skeleton lines={4} /><SkeletonGrid n={3} /></div>;
  const problems: string[] = [];
  for (const c of h.cameras) {
    if (!c.stream_ready) problems.push(`${c.name} is not recording`);
    for (const p of c.health?.problems ?? []) problems.push(`${c.name}: ${p}`);
    if (c.stream_ready && c.metadata_last && h.now - c.metadata_last > 1800) problems.push(`${c.name}: no detections for ${Math.round((h.now - c.metadata_last) / 60)} min`);
  }
  if (h.retention_alert) problems.push("Retention can't hold the continuous window (disk full)");
  if (!h.yolo_ready) problems.push("YOLO is still loading");
  if (!h.vlm_ready) problems.push("Qwen is still loading");
  const healthy = problems.length === 0;
  const usedPct = Math.round((1 - h.disk.free_gb / h.disk.total_gb) * 100);

  return (
    <div className="view home">
      <div className={`home-status ${healthy ? "ok" : "warn"}`}>
        <span className={`dot ${healthy ? "ok" : "bad"}`} />
        {healthy
          ? <span>All {h.cameras.length} cameras recording · {h.disk.free_gb.toLocaleString()} GB free ({usedPct}% used)</span>
          : <span>{problems.join(" · ")}</span>}
        <span className="spacer" />
        <button className="ghost small" onClick={() => onGo("Settings")}>Settings</button>
        <button className="ghost small" onClick={() => onGo("Live")}>Live view</button>
      </div>

      <BriefingCard onEvent={setOpen} compact />

      <section>
        <h3>Needs attention <span className="muted small">since {fmtTime(h.since)} · {h.new_since} new sightings</span></h3>
        {h.attention.length === 0
          ? <div className="empty">Nothing unusual or elevated since you last looked.</div>
          : <div className="event-grid">{h.attention.map((e: NvrEvent) => <EventCard key={e.id} e={e} cameraName={name(e.camera_id)} onOpen={() => setOpen(e.id)} />)}</div>}
      </section>

      <div className="home-grid">
        <section>
          <h3>Cameras today</h3>
          <table className="kv">
            <tbody>
              {h.cameras.map((c) => (
                <tr key={c.id}>
                  <td><span className={`dot ${c.stream_ready ? "ok" : "bad"}`} /> {c.name}</td>
                  <td>
                    {Object.entries(c.today).map(([k, n]) => `${n} ${k}${n === 1 ? "" : "s"}`).join(", ") || "quiet"}
                    <span className="muted small"> · last detection {c.metadata_last ? ago(c.metadata_last) : "never"}
                      {c.health?.bitrate_mbps != null && ` · ${c.health.bitrate_mbps.toFixed(1)} Mbps`}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="muted small">
            {h.baseline.some((b) => b.learning) ? "Still learning what's normal on: " + h.baseline.filter((b) => b.learning).map((b) => name(b.camera_id)).join(", ") + "." : "Baselines active on every camera."}
            {h.queues.verify + h.queues.synopsis > 0 && ` · ${h.queues.verify} verifying, ${h.queues.synopsis} awaiting Qwen`}
            {h.backup && ` · last backup ${ago(h.backup.at)}`}
          </div>
        </section>
        <section>
          <h3>Latest <button className="linkish small" onClick={() => onGo("Events")}>all events →</button></h3>
          <div className="home-recent">
            {h.recent.map((e: NvrEvent) => <EventCard key={e.id} e={e} cameraName={name(e.camera_id)} onOpen={() => setOpen(e.id)} />)}
          </div>
        </section>
      </div>
      {open !== null && <EventDetail id={open} cameraName={name} onClose={() => setOpen(null)} />}
    </div>
  );
}
