import { useEffect, useState } from "react";
import { api, type RetentionPolicy, type RetentionPreview, type RetentionStats } from "./api";

const KEEP_LABELS: [keyof RetentionPolicy["keep"], string, string][] = [
  ["person", "People", "Verified person events"],
  ["qwen_analyzed", "Qwen-analyzed events", "Anything with a synopsis (threat level raises its priority)"],
  ["rule_events", "Camera rule events", "Intrusion, line crossing, loitering, tamper… (see topics)"],
  ["feedback", "Events you gave feedback on", "Ratings, verdicts, corrections, saved chat notes"],
  ["vehicles_in_detect_zones", "Vehicles in detect-only areas", "Only when the camera has 'detect only' zones"],
];
const ALL_TOPICS = ["FieldDetector", "LineDetector", "LoiteringDetector", "FieldInDetector", "FieldOutDetector",
  "TamperDetector", "ObjectLeftDetector", "ObjectRemoveDetector", "MotionDetector", "CellMotionDetector"];

/** Policy editor, used for the site policy and for per-camera overrides. */
export function PolicyForm({ value, onChange, showFloor = true }: { value: RetentionPolicy; onChange: (p: RetentionPolicy) => void; showFloor?: boolean }) {
  const set = (patch: Partial<RetentionPolicy>) => onChange({ ...value, ...patch });
  const setKeep = (k: keyof RetentionPolicy["keep"], v: boolean) => onChange({ ...value, keep: { ...value.keep, [k]: v } });
  const num = (v: string, min: number, max: number) => Math.min(max, Math.max(min, Number(v) || min));
  return (
    <div className="form policy-form">
      <div className="row">
        <label className="field"><span>Continuous recording (days)</span>
          <input type="number" min={1} max={365} value={value.continuous_days} onChange={(e) => set({ continuous_days: num(e.target.value, 1, 365) })} />
        </label>
        <label className="field"><span>Keep before event (s)</span>
          <input type="number" min={0} max={600} value={value.pad_before_s} onChange={(e) => set({ pad_before_s: num(e.target.value, 0, 600) })} />
        </label>
        <label className="field"><span>Keep after event (s)</span>
          <input type="number" min={0} max={600} value={value.pad_after_s} onChange={(e) => set({ pad_after_s: num(e.target.value, 0, 600) })} />
        </label>
        {showFloor && (
          <label className="field"><span>Free-space floor (GB)</span>
            <input type="number" min={10} max={100000} value={value.min_free_gb} onChange={(e) => set({ min_free_gb: num(e.target.value, 10, 100000) })} />
          </label>
        )}
      </div>
      <div className="field">
        <span>After {value.continuous_days} days, keep footage around</span>
        <div className="keep-list">
          <label className="keep-item locked"><input type="checkbox" checked disabled /> <strong>Locked clips</strong> <span className="muted small">always, never deleted</span></label>
          {KEEP_LABELS.map(([k, label, hint]) => (
            <label key={k} className="keep-item">
              <input type="checkbox" checked={value.keep[k]} onChange={(e) => setKeep(k, e.target.checked)} />
              <strong>{label}</strong> <span className="muted small">{hint}</span>
            </label>
          ))}
        </div>
      </div>
      {value.keep.rule_events && (
        <div className="field">
          <span>Rule topics that count</span>
          <div className="chips">
            {ALL_TOPICS.map((t) => {
              const on = value.rule_topics.includes(t);
              return (
                <button key={t} type="button" className={`chip ${on ? "on-person" : ""}`}
                  onClick={() => set({ rule_topics: on ? value.rule_topics.filter((x) => x !== t) : [...value.rule_topics, t] })}>
                  {t.replace("Detector", "")}
                </button>
              );
            })}
          </div>
        </div>
      )}
      <p className="muted small">Everything else older than {value.continuous_days} days is deleted. Kept footage stays until the disk needs space; then the least important and oldest goes first (routine vehicles before people, people before threats). Locked footage is never deleted.</p>
    </div>
  );
}

export function RetentionPanel() {
  const [policy, setPolicy] = useState<RetentionPolicy | null>(null);
  const [saved, setSaved] = useState<string>("");
  const [stats, setStats] = useState<RetentionStats | null>(null);
  const [previews, setPreviews] = useState<Record<string, RetentionPreview>>({});
  const [msg, setMsg] = useState("");

  const load = async () => {
    const s = await api.retentionStats();
    setStats(s);
    const entries = await Promise.all(s.cameras.map(async (c) => [c.camera_id, await api.retentionPreview(c.camera_id)] as const));
    setPreviews(Object.fromEntries(entries));
  };
  useEffect(() => {
    api.retentionPolicy().then((r) => {
      setPolicy(r.policy);
      setSaved(JSON.stringify(r.policy));
    });
    load().catch(() => {});
    const t = setInterval(() => load().catch(() => {}), 60000);
    return () => clearInterval(t);
  }, []);

  if (!policy || !stats) return <div className="stat muted">Loading retention…</div>;
  const dirty = JSON.stringify(policy) !== saved;
  const used = stats.disk.total_gb - stats.disk.free_gb;
  const cont = stats.cameras.reduce((a, c) => a + c.continuous_gb, 0);
  const kept = stats.cameras.reduce((a, c) => a + c.kept_gb - c.locked_gb, 0);
  const locked = stats.cameras.reduce((a, c) => a + c.locked_gb, 0);
  const other = Math.max(0, used - cont - kept - locked);
  const pct = (gb: number) => `${(gb / stats.disk.total_gb) * 100}%`;
  const perDay = stats.cameras.reduce((a, c) => a + c.gb_per_day, 0);

  return (
    <div className="retention">
      <div className="section-head">
        <h2>Retention</h2>
        {stats.dry_run && <span className="badge status-open">Dry run: nothing is deleted</span>}
      </div>
      {stats.alert && <div className="alert-box">{stats.alert.message} ({stats.alert.free_gb} GB free, floor {stats.alert.floor_gb} GB)</div>}

      <div className="stat">
        <div className="muted small">Recording disk</div>
        <div className="stack-bar">
          <div className="seg cont" style={{ width: pct(cont) }} title={`Continuous ${cont.toFixed(1)} GB`} />
          <div className="seg kept" style={{ width: pct(kept) }} title={`AI-kept ${kept.toFixed(1)} GB`} />
          <div className="seg locked" style={{ width: pct(locked) }} title={`Locked ${locked.toFixed(1)} GB`} />
          <div className="seg other" style={{ width: pct(other) }} title={`Other files ${other.toFixed(0)} GB`} />
        </div>
        <div className="legend small muted">
          <span><i className="sw cont" /> continuous {cont.toFixed(1)} GB</span>
          <span><i className="sw kept" /> AI-kept {kept.toFixed(1)} GB</span>
          <span><i className="sw locked" /> locked {locked.toFixed(1)} GB</span>
          <span><i className="sw other" /> other {other.toFixed(0)} GB</span>
          <span>free {stats.disk.free_gb.toLocaleString()} GB of {stats.disk.total_gb.toLocaleString()} GB</span>
          {perDay > 0 && <span>· recording ≈ {perDay.toFixed(1)} GB/day, {policy.continuous_days} days ≈ {(perDay * policy.continuous_days).toFixed(0)} GB</span>}
        </div>
      </div>

      <table className="table">
        <thead>
          <tr><th>Camera</th><th>Continuous on disk</th><th>Continuous</th><th>AI-kept</th><th>Locked</th><th>GB/day</th><th>Next 24 h of age-outs</th></tr>
        </thead>
        <tbody>
          {stats.cameras.map((c) => {
            const p = previews[c.camera_id];
            return (
              <tr key={c.camera_id}>
                <td><strong>{c.name}</strong>{JSON.stringify(c.policy) !== JSON.stringify(policy) && <span className="badge" title="This camera overrides the site policy">custom</span>}</td>
                <td>{c.continuous_days_on_disk.toFixed(1)} of {c.policy.continuous_days} days</td>
                <td>{c.continuous_gb.toFixed(1)} GB</td>
                <td>{c.kept_gb.toFixed(1)} GB <span className="muted small">({c.kept_files} clips)</span></td>
                <td>{c.locked_gb.toFixed(1)} GB</td>
                <td>{c.gb_per_day.toFixed(1)}</td>
                <td className="small">
                  {!p ? "…" : p.segments === 0 ? <span className="muted">nothing ages out yet</span> : (
                    <>keep {p.kept_minutes} min, delete {p.deleted_minutes} min (≈{p.freed_gb} GB)
                      {p.deferred > 0 && <span className="muted"> · {p.deferred} waiting on AI</span>}
                      {Object.keys(p.kept_minutes_by_reason).length > 0 && (
                        <div className="muted">{Object.entries(p.kept_minutes_by_reason).map(([k, v]) => `${k} ${v}m`).join(" · ")}</div>
                      )}
                    </>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      <div className="stat">
        <h3>Site policy</h3>
        <PolicyForm value={policy} onChange={setPolicy} />
        <div className="row">
          <button disabled={!dirty} onClick={async () => {
            const r = await api.saveRetentionPolicy(policy);
            setPolicy(r.policy);
            setSaved(JSON.stringify(r.policy));
            setMsg("Saved. Applies on the next retention pass (every 10 minutes).");
            load();
          }}>Save policy</button>
          {dirty && <button className="ghost" onClick={() => setPolicy(JSON.parse(saved))}>Revert</button>}
          {msg && <span className="muted small">{msg}</span>}
        </div>
        <p className="muted small">Cameras can override this under Cameras → Edit → Retention.</p>
      </div>
    </div>
  );
}
