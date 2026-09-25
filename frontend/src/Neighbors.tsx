import { useEffect, useState } from "react";
import { api, type Camera, type CameraLink, type LinkSuggestion } from "./api";

/**
 * Cameras next to this one and the walking time between them (used to link the same person across
 * cameras). The topology is site-wide; this editor shows and edits the links involving `cameraId`.
 */
export function NeighborsEditor({ cameraId, cameras }: { cameraId: string; cameras: Camera[] }) {
  const [all, setAll] = useState<CameraLink[] | null>(null);
  const [suggestions, setSuggestions] = useState<LinkSuggestion[]>([]);
  const [msg, setMsg] = useState("");
  const name = (id: string) => cameras.find((c) => c.id === id)?.name ?? id;

  useEffect(() => {
    api.topology().then(setAll).catch(() => setAll([]));
    api.topologySuggestions().then(setSuggestions).catch(() => {});
  }, []);
  if (!all) return <p className="muted small">Loading neighbours…</p>;

  const mine = all.map((l, i) => ({ l, i })).filter(({ l }) => l.cam_a === cameraId || l.cam_b === cameraId);
  const others = cameras.filter((c) => c.id !== cameraId);
  const update = (i: number, patch: Partial<CameraLink>) => setAll(all.map((l, j) => (j === i ? { ...l, ...patch } : l)));
  const save = async (links: CameraLink[]) => {
    const r = await api.saveTopology(links);
    setAll(r.links);
    setMsg(`Saved. Re-checking ${r.relinking} recent person events for cross-camera matches.`);
    api.topologySuggestions().then(setSuggestions).catch(() => {});
  };
  const pending = suggestions.filter((s) => !s.configured && (s.cam_a === cameraId || s.cam_b === cameraId));

  return (
    <div className="neighbors">
      <p className="muted small">
        Cameras someone can walk to from here, and how long it usually takes. When a person leaves one camera and a similar-looking
        person appears on a neighbour inside that window, re-ID and Qwen check whether it's the same individual and link the two
        events into a journey. Use a negative minimum if the views overlap.
      </p>
      {mine.length === 0 && <p className="muted small">No neighbours yet.</p>}
      {mine.map(({ l, i }) => {
        const other = l.cam_a === cameraId ? l.cam_b : l.cam_a;
        const outgoing = l.cam_a === cameraId;
        return (
          <div key={i} className="neighbor-row">
            <select value={other} onChange={(e) => update(i, outgoing ? { cam_b: e.target.value } : { cam_a: e.target.value })}>
              {others.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            </select>
            <label className="small">walk <input type="number" value={l.min_s} onChange={(e) => update(i, { min_s: +e.target.value })} /></label>
            <label className="small">to <input type="number" value={l.max_s} onChange={(e) => update(i, { max_s: +e.target.value })} /> s</label>
            <label className="small row" title={outgoing ? `Only from ${name(cameraId)} to ${name(other)}` : `Only from ${name(other)} to ${name(cameraId)}`}>
              <input type="checkbox" checked={Boolean(l.one_way)} onChange={(e) => update(i, { one_way: e.target.checked })} />
              one-way {l.one_way ? (outgoing ? "→" : "←") : ""}
            </label>
            <button className="ghost small" onClick={() => setAll(all.filter((_, j) => j !== i))} aria-label="Remove">✕</button>
          </div>
        );
      })}
      <div className="row">
        {others.length > 0 && (
          <button className="ghost small" onClick={() => setAll([...all, { cam_a: cameraId, cam_b: others[0].id, min_s: 0, max_s: 60, one_way: false }])}>
            Add neighbour
          </button>
        )}
        <button className="small" onClick={() => save(all)}>Save neighbours</button>
      </div>
      {pending.length > 0 && (
        <div className="suggestions">
          <div className="small"><strong>Suggested from history</strong> <span className="muted">(similar-looking people seen on both cameras close together)</span></div>
          {pending.map((s) => (
            <div key={`${s.cam_a}-${s.cam_b}`} className="neighbor-row suggestion">
              <span className="small">{name(s.cam_a)} → {name(s.cam_b)}: seen {s.count}×, typical walk {s.median_gap_s} s</span>
              <button className="ghost small" onClick={() => save([...all, { cam_a: s.cam_a, cam_b: s.cam_b, min_s: s.min_s, max_s: s.max_s, one_way: false }])}>
                Accept ({s.min_s}–{s.max_s} s)
              </button>
            </div>
          ))}
        </div>
      )}
      {msg && <p className="small">{msg}</p>}
    </div>
  );
}
