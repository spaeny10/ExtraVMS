/**
 * Settings → System → "Optimize my system": measured suggestions with Qwen's one-paragraph read on top.
 * Each card says what to change, why (the measurement), what improves, and either the camera-side steps or
 * an Apply button when the NVR can do it itself. Dismissed cards stay hidden until the measurement changes.
 */
import { useState } from "react";
import { api, type AdvisorFinding, type AdvisorReport } from "./api";
import { Icon, toast } from "./ui";

const AREA_LABEL: Record<string, string> = { cameras: "Cameras", storage: "Storage", ai: "AI", events: "Detections", rules: "Zones & rules", ptz: "PTZ", time: "Clocks" };
const IMPACT_LABEL: Record<string, string> = { high: "Fix soon", medium: "Worth doing", low: "Nice to have" };

export function Advisor({ onAsk }: { onAsk?: (q: string) => void }) {
  const [report, setReport] = useState<AdvisorReport | null>(null);
  const [busy, setBusy] = useState(false);
  const [showHidden, setShowHidden] = useState(false);
  const [summarizing, setSummarizing] = useState(false);
  // measurements first (a second or two), then Qwen's paragraph fills in when it arrives
  const run = async () => {
    setBusy(true);
    try {
      const quick = await api.advisor(false);
      setReport(quick);
      setBusy(false);
      if (quick.findings.length) {
        setSummarizing(true);
        try { const full = await api.advisor(true); setReport((r) => r && { ...r, summary: full.summary }); }
        catch { /* the plain summary stays */ }
        finally { setSummarizing(false); }
      }
    } catch (e) { toast.error(e); setBusy(false); }
  };
  const dismiss = async (f: AdvisorFinding) => {
    try { await api.advisorDismiss(f.key, f.fingerprint); setReport((r) => r && { ...r, findings: r.findings.filter((x) => x.key !== f.key), hidden: [...r.hidden, f] }); }
    catch (e) { toast.error(e); }
  };
  const restore = async (f: AdvisorFinding) => {
    try { await api.advisorUndismiss(f.key); setReport((r) => r && { ...r, hidden: r.hidden.filter((x) => x.key !== f.key), findings: [...r.findings, f] }); }
    catch (e) { toast.error(e); }
  };
  const apply = async (f: AdvisorFinding) => {
    if (!f.apply) return;
    try {
      const r = await api.advisorApply(f.apply);
      toast.success(r.message || "Applied");
      setReport((rep) => rep && { ...rep, findings: rep.findings.filter((x) => x.key !== f.key) });
    } catch (e) { toast.error(e); }
  };

  return (
    <section className="sys-group adv">
      <div className="adv-hero">
        <div className="adv-hero-text">
          <h3><Icon name="sparkle" size={18} /> Optimize my system</h3>
          <p className="muted small">Measures your cameras, storage, detections and the AI, then suggests what to change and why. Nothing changes until you apply it.</p>
        </div>
        <button className="ask-btn" disabled={busy} onClick={run}>{busy ? "Measuring…" : report ? "Check again" : "✦ Check my system"}</button>
      </div>

      {report && (
        <>
          <div className={`adv-summary ${report.findings.length === 0 ? "ok" : ""}`}>
            <p>{report.summary.text}</p>
            <div className="muted small adv-meta">
              {report.summary.model ? <span className="model-tag">{report.summary.model}</span> : summarizing ? <span>✦ Qwen is writing its read…</span> : <span>plain summary</span>}
              <span> · {report.cameras} cameras</span>
              {report.facts.gb_per_day != null && <span> · {report.facts.gb_per_day} GB/day</span>}
              {report.facts.median_synopsis_s != null && <span> · synopses {report.facts.median_synopsis_s} s</span>}
              {report.facts.vlm.vram_gb != null && <span> · Qwen {report.facts.vlm.vram_gb}/{report.facts.vlm.size_gb} GB in VRAM</span>}
              {onAsk && <button className="linkish small" onClick={() => onAsk("What should I change to make my camera system run better?")}>Ask Qwen to explain →</button>}
            </div>
          </div>

          {report.findings.length > 0 && (
            <div className="adv-list">
              {report.findings.map((f) => (
                <article key={f.key} className={`adv-card impact-${f.impact}`}>
                  <header>
                    <span className={`adv-impact ${f.impact}`}>{IMPACT_LABEL[f.impact]}</span>
                    <span className="adv-area">{AREA_LABEL[f.area] ?? f.area}</span>
                    <span className="spacer" />
                    <button className="ghost small" title="Hide until the measurement changes" onClick={() => dismiss(f)}>Dismiss</button>
                  </header>
                  <h4>{f.title}</h4>
                  <p className="adv-why">{f.why}</p>
                  <p className="adv-effect"><strong>Gain:</strong> {f.effect}</p>
                  {f.steps.length > 0 && <ol className="adv-steps">{f.steps.map((s, i) => <li key={i}>{s}</li>)}</ol>}
                  <div className="row">
                    {f.apply && <button className="small" onClick={() => apply(f)}>Apply</button>}
                    {onAsk && <button className="ghost small" onClick={() => onAsk(`On ${f.camera ?? "my system"}: ${f.title}. Why does this matter and what exactly should I change?`)}>✦ Ask about this</button>}
                  </div>
                </article>
              ))}
            </div>
          )}

          {report.hidden.length > 0 && (
            <div className="adv-hidden">
              <button className="linkish small" onClick={() => setShowHidden(!showHidden)}>{showHidden ? "▾" : "▸"} {report.hidden.length} dismissed</button>
              {showHidden && report.hidden.map((f) => (
                <div key={f.key} className="row small muted"><span>{f.title}</span><span className="spacer" /><button className="linkish small" onClick={() => restore(f)}>Show again</button></div>
              ))}
            </div>
          )}
        </>
      )}
    </section>
  );
}
