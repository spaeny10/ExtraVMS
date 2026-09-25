import { useEffect, useRef, useState } from "react";
import { api, fmtTime, frameUrl, type Camera, type FootageMatch, type FootageMoment } from "./api";
import { useNav } from "./nav";
import { SkeletonGrid } from "./ui";

/** How many of the best results Qwen double-checks after they render. */
const VERIFY_TOP = 8;

type Checked = FootageMoment & { check?: FootageMatch | "pending" | "error" };

/** Results from the image-text index of all recorded footage (not just events). */
export function FootageResults({ q, nonce, cameras, camera, sinceHours, window: win }: {
  q: string; nonce: number; cameras: Camera[]; camera: string; sinceHours: number;
  /** a window read from the query ("today"); overrides sinceHours */
  window?: { since: number | null; until: number | null } | null;
}) {
  const [results, setResults] = useState<Checked[] | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const run = useRef(0);
  const { openInTimeline } = useNav();
  const name = (id: string) => cameras.find((c) => c.id === id)?.name ?? id;

  useEffect(() => {
    if (!q.trim() || !nonce) return;
    const my = ++run.current;
    setBusy(true); setErr(""); setResults(null);
    const since = win?.since ?? (sinceHours ? Date.now() / 1000 - sinceHours * 3600 : undefined);
    api.footageSearch(q, camera || undefined, since, win?.until ?? undefined).then(async (ms) => {
      if (run.current !== my) return;
      setResults(ms);
      setBusy(false);
      // Qwen checks the best few, one at a time; confirmed ones move up, rejected ones are dimmed.
      for (const m of ms.slice(0, VERIFY_TOP)) {
        if (run.current !== my) return;
        setResults((rs) => rs && rs.map((r) => (r === m || same(r, m) ? { ...r, check: "pending" } : r)));
        let check: Checked["check"];
        try { check = await api.footageVerify({ camera_id: m.camera_id, ts: m.ts, tile: m.tile, q }); }
        catch { check = "error"; }
        if (run.current !== my) return;
        setResults((rs) => rs && rs.map((r) => (same(r, m) ? { ...r, check } : r)));
      }
    }).catch((e) => { if (run.current === my) { setErr(String(e)); setBusy(false); } });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nonce, camera, sinceHours]);

  if (busy) return <SkeletonGrid n={4} />;
  if (err) return <div className="error">{err}</div>;
  if (!results) return null;
  if (!results.length) return <div className="empty">Nothing in the indexed footage looks like that.</div>;
  const scores = results.map((r) => r.score);
  const lo = Math.min(...scores), hi = Math.max(...scores);
  const rank = (r: Checked) => (typeof r.check === "object" ? (r.check.matches ? 0 : 2) : 1);
  const ordered = [...results].sort((a, b) => rank(a) - rank(b));
  return (
    <div className="event-grid">
      {ordered.map((m) => {
        const c = typeof m.check === "object" ? m.check : null;
        const [x0, y0, x1, y1] = m.box;
        return (
          <button key={`${m.camera_id}${m.ts}`} className={`event-card footage-card${c && !c.matches ? " dim" : ""}`}
            onClick={() => openInTimeline({ id: 0, camera_id: m.camera_id, start_ts: m.ts, end_ts: Math.max(m.end, m.ts + 5), camera_class: "moment" })}
            title="Open on the Timeline at this moment">
            <div className="thumb">
              <img src={frameUrl(m.camera_id, m.ts, 480)} loading="lazy" alt="" />
              {m.tile !== 0 && <div className="footage-box" style={{ left: `${x0 * 100}%`, top: `${y0 * 100}%`, width: `${(x1 - x0) * 100}%`, height: `${(y1 - y0) * 100}%` }} />}
            </div>
            <div className="event-body">
              <div className="event-meta">
                <span className="cam">{name(m.camera_id)}</span>
                <span className="muted">{fmtTime(m.ts)}{m.end - m.start >= 5 ? ` · ${Math.round(m.end - m.start)} s` : ""}</span>
              </div>
              <div className="match-bar" title={`Similarity ${m.score.toFixed(3)} (relative to these results)`}>
                <div style={{ width: `${hi > lo ? 15 + 85 * (m.score - lo) / (hi - lo) : 100}%` }} />
              </div>
              <div className="event-foot">
                {m.check === "pending" && <span className="muted small">Qwen is checking…</span>}
                {m.check === "error" && <span className="muted small">Qwen check unavailable</span>}
                {c && (c.matches
                  ? <span className="badge status-verified" title={`Qwen (${c.confidence} confidence)`}>✓ {c.seen || "Qwen confirmed"}</span>
                  : <span className="muted small" title={`Qwen (${c.confidence} confidence)`}>✗ Qwen: {c.seen || "not a match"}</span>)}
              </div>
            </div>
          </button>
        );
      })}
    </div>
  );
}

const same = (a: FootageMoment, b: FootageMoment) => a.camera_id === b.camera_id && a.ts === b.ts;
