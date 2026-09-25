import { useEffect, useRef, useState } from "react";
import { api, type Camera, type FeedbackStats } from "./api";

/**
 * Explains, accurately, how operator feedback changes Qwen's output:
 * scene notes and recent corrections go into the prompt now; ratings/verdicts only build a dataset.
 * (See synopsis.build_prompt / event_facts and Pipeline.correction_examples in the backend.)
 */
export function QwenFeedbackInfo() {
  const [open, setOpen] = useState(false);
  const [stats, setStats] = useState<FeedbackStats | null>(null);
  const [cameras, setCameras] = useState<Camera[]>([]);
  const root = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    api.feedbackStats().then(setStats).catch(() => {});
    api.cameras().then(setCameras).catch(() => {});
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    const onDown = (e: MouseEvent) => {
      if (root.current && !root.current.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    window.addEventListener("mousedown", onDown);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("mousedown", onDown);
    };
  }, [open]);

  const withNotes = cameras.filter((c) => (c.scene_notes ?? "").trim());

  return (
    <div className="info-anchor" ref={root}>
      <button className="info-btn" onClick={() => setOpen(!open)} aria-expanded={open} title="How feedback improves Qwen">
        ⓘ How feedback improves Qwen
      </button>
      {open && (
        <div className="popout" role="dialog" aria-label="How feedback improves Qwen">
          <div className="popout-head">
            <strong>How your feedback improves Qwen</strong>
            <button className="ghost small" onClick={() => setOpen(false)} aria-label="Close">✕</button>
          </div>
          <p className="muted small">
            Qwen's weights are not retrained here. Your feedback steers it through the prompt, right away, and builds a
            dataset you can use for real training later.
          </p>

          <section>
            <h4>1 · Used by Qwen right away</h4>
            <div className="info-row">
              <span className="info-icon">📝</span>
              <div>
                <strong>Scene notes</strong> (Cameras → Edit) are added to every synopsis and chat prompt for that camera.
                Best for standing facts: "the trailers are our solar towers", "highway traffic is routine".
                <div className="info-status">
                  {cameras.length ? `${withNotes.length} of ${cameras.length} camera${cameras.length > 1 ? "s have" : " has"} notes` : "…"}
                  {cameras.map((c) => (
                    <span key={c.id} className={`pill ${(c.scene_notes ?? "").trim() ? "ok" : ""}`}>{(c.scene_notes ?? "").trim() ? "✓" : "–"} {c.name}</span>
                  ))}
                </div>
              </div>
            </div>
            <div className="info-row">
              <span className="info-icon">✏️</span>
              <div>
                <strong>Corrected synopses</strong> (event → Edit): the 3 most recent corrections on a camera are shown to
                Qwen as examples ("the model wrote X, the operator corrected it to Y"), so it copies your style, detail
                and threat judgement.
                <div className="info-status">{stats ? `${stats.corrected} correction${stats.corrected === 1 ? "" : "s"} so far` : "…"}</div>
              </div>
            </div>
            <p className="muted small">Changes apply to the next synopsis. Use <em>Regenerate</em> on an event to see the effect.</p>
          </section>

          <section>
            <h4>2 · Recorded for future training</h4>
            <div className="info-row">
              <span className="info-icon">📦</span>
              <div>
                👍/👎 with reasons, detection verdicts (correct / false alarm / wrong class), notes and corrections are saved
                as a labelled dataset. <strong>Qwen doesn't read these yet.</strong> They can later be used to fine-tune Qwen
                (a LoRA adapter) or YOLO offline. That's a separate, manual step, not automatic.
                <div className="info-status">
                  {stats ? `👍 ${stats.up} · 👎 ${stats.down}` : "…"}
                  <a href="/api/feedback/export">Export dataset (JSONL)</a>
                </div>
              </div>
            </div>
          </section>

          <section>
            <h4>3 · What helps most</h4>
            <ul className="plain small">
              <li><strong>Fix</strong> a wrong synopsis with Edit instead of only 👎. Corrections are what Qwen learns from today.</li>
              <li>Put anything Qwen gets wrong repeatedly into <strong>scene notes</strong>.</li>
              <li>Mark <strong>false alarms</strong>. They build the dataset for tuning detection.</li>
            </ul>
            <p className="muted small">Also: events you gave feedback on are kept past the continuous-recording window by retention.</p>
            <p className="muted small">
              <strong>Cross-camera journeys:</strong> a re-ID model matches a person's appearance on neighbouring cameras, then Qwen
              confirms the match. <em>Not the same person</em> on an event's journey splits it, and Qwen is never asked about that pair again.
            </p>
          </section>
        </div>
      )}
    </div>
  );
}
