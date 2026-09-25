import { useEffect, useMemo, useRef, useState } from "react";
import { api, frameUrl, zoneAllowed, type Camera, type DetectionPoint, type Zone } from "./api";

type Pt = [number, number];
const ASPECT = 2592 / 1520; // main stream frame
const VB_W = 1000;
const VB_H = VB_W / ASPECT;

/**
 * Full-size editor for include / exclude detection zones, drawn on a still from the main stream,
 * with recent detections overlaid so busy areas (a highway) are obvious and the effect is visible live.
 */
export function ZoneEditor({ camera, onClose, onSaved }: { camera: Camera; onClose: () => void; onSaved: () => void }) {
  const [zones, setZones] = useState<Zone[]>(() => camera.zones.map((z) => ({ ...z, type: z.type ?? "include", points: [...z.points] })));
  const [draft, setDraft] = useState<{ type: "include" | "exclude" | "area"; points: Pt[] } | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [cursor, setCursor] = useState<Pt | null>(null);
  const [stillT, setStillT] = useState(() => Date.now() / 1000 - 5);
  const [dets, setDets] = useState<DetectionPoint[]>([]);
  const [showDets, setShowDets] = useState(true);
  const [preview, setPreview] = useState<{ mask: number; restore: number } | null>(null);
  const [saving, setSaving] = useState(false);
  const [result, setResult] = useState("");
  const svg = useRef<SVGSVGElement>(null);
  const dragging = useRef<{ zone: number; point: number } | null>(null);

  useEffect(() => {
    api.detections(camera.id).then(setDets).catch(() => setDets([]));
  }, [camera.id]);

  // Preview how many past events the current (unsaved) zones would hide or restore.
  useEffect(() => {
    const t = setTimeout(() => api.previewZones(camera.id, zones).then(setPreview).catch(() => setPreview(null)), 400);
    return () => clearTimeout(t);
  }, [zones, camera.id]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        if (draft) setDraft(null);
        else onClose();
      }
      if ((e.key === "Delete" || e.key === "Backspace") && selected != null && !draft && !(e.target instanceof HTMLInputElement)) {
        setZones((zs) => zs.filter((_, i) => i !== selected));
        setSelected(null);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [draft, selected, onClose]);

  const toNorm = (ev: React.PointerEvent | React.MouseEvent): Pt => {
    const r = svg.current!.getBoundingClientRect();
    const clamp = (v: number) => Math.min(1, Math.max(0, v));
    return [clamp((ev.clientX - r.left) / r.width), clamp((ev.clientY - r.top) / r.height)];
  };
  const toVB = ([x, y]: Pt) => `${x * VB_W},${y * VB_H}`;
  const closeDraft = () => {
    if (!draft) return;
    // A double-click also delivers two clicks; drop the near-duplicate points they add.
    const pts = draft.points.filter((p, i, a) => i === 0 || Math.hypot(p[0] - a[i - 1][0], p[1] - a[i - 1][1]) > 0.004);
    if (pts.length < 3) return;
    const n = zones.filter((z) => z.type === draft.type).length + 1;
    setZones([...zones, { name: `${draft.type === "exclude" ? "Mask" : draft.type === "area" ? "Place" : "Area"} ${n}`, type: draft.type, points: pts }]);
    setSelected(zones.length);
    setDraft(null);
  };

  const onCanvasClick = (ev: React.MouseEvent) => {
    if (!draft) {
      setSelected(null);
      return;
    }
    const p = toNorm(ev);
    // Clicking near the first point closes the shape.
    if (draft.points.length >= 3) {
      const [fx, fy] = draft.points[0];
      const r = svg.current!.getBoundingClientRect();
      if (Math.hypot((p[0] - fx) * r.width, (p[1] - fy) * r.height) < 12) return closeDraft();
    }
    setDraft({ ...draft, points: [...draft.points, p] });
  };

  const onMove = (ev: React.PointerEvent) => {
    const p = toNorm(ev);
    setCursor(p);
    const d = dragging.current;
    if (d) setZones((zs) => zs.map((z, i) => (i === d.zone ? { ...z, points: z.points.map((pt, j) => (j === d.point ? p : pt)) } : z)));
  };

  // Recent detections, classified with the zones as currently drawn.
  const classified = useMemo(
    () => dets.map((d) => ({ d, ok: zoneAllowed(d[0], d[1], zones) })),
    [dets, zones],
  );
  const ignored = classified.filter((c) => !c.ok).length;

  const save = async () => {
    setSaving(true);
    setResult("");
    try {
      const { status: _status, ...cam } = camera;
      await api.saveCamera({ ...cam, zones });
      const r = await api.applyZones(camera.id);
      setResult(`Saved. Hid ${r.masked} past event${r.masked === 1 ? "" : "s"}${r.restored ? `, restored ${r.restored}` : ""}.`);
      onSaved();
    } catch (e) {
      setResult(`Save failed: ${e}`);
    } finally {
      setSaving(false);
    }
  };

  const setZone = (i: number, patch: Partial<Zone>) => setZones(zones.map((z, j) => (j === i ? { ...z, ...patch } : z)));

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal zone-modal" onClick={(e) => e.stopPropagation()}>
        <header className="modal-head">
          <div>
            <h2>Detection zones · {camera.name}</h2>
            <span className="muted small">Objects count where they touch the ground (bottom-centre of the box). Masked areas never create events and are hidden from YOLO.</span>
          </div>
          <button className="ghost" onClick={onClose} aria-label="Close">✕</button>
        </header>
        <div className="zone-layout">
          <div>
            <div className="zone-stage" style={{ aspectRatio: `${ASPECT}` }}>
              <img src={frameUrl(camera.id, stillT, 1280)} alt="" draggable={false} />
              <svg
                ref={svg}
                viewBox={`0 0 ${VB_W} ${VB_H}`}
                preserveAspectRatio="none"
                className={draft ? "drawing" : ""}
                onClick={onCanvasClick}
                onDoubleClick={(e) => { e.preventDefault(); closeDraft(); }}
                onPointerMove={onMove}
                onPointerUp={() => (dragging.current = null)}
                onPointerLeave={() => setCursor(null)}
              >
                <defs>
                  <pattern id="hatch" width="12" height="12" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
                    <rect width="12" height="12" fill="rgb(239 68 68 / .28)" />
                    <line x1="0" y1="0" x2="0" y2="12" stroke="rgb(239 68 68 / .7)" strokeWidth="4" />
                  </pattern>
                </defs>
                {showDets && classified.map(({ d, ok }, i) => (
                  <circle key={i} cx={d[0] * VB_W} cy={d[1] * VB_H} r={ok ? 3.2 : 2.4}
                    className={`det ${ok ? "kept" : "masked"} ${d[2]}`} />
                ))}
                {zones.map((z, i) => (
                  <g key={i} className={`zone-shape ${z.type} ${selected === i ? "selected" : ""}`}>
                    <polygon points={z.points.map(toVB).join(" ")}
                      onClick={(e) => { if (!draft) { e.stopPropagation(); setSelected(i); } }} />
                    {selected === i && !draft && z.points.map((p, j) => (
                      <circle key={j} cx={p[0] * VB_W} cy={p[1] * VB_H} r={7} className="vertex"
                        onClick={(e) => e.stopPropagation()}
                        onPointerDown={(e) => { e.stopPropagation(); (e.target as Element).setPointerCapture(e.pointerId); dragging.current = { zone: i, point: j }; }} />
                    ))}
                  </g>
                ))}
                {draft && (
                  <g className={`zone-shape draft ${draft.type}`}>
                    <polyline points={[...draft.points, ...(cursor ? [cursor] : [])].map(toVB).join(" ")} />
                    {draft.points.map((p, j) => <circle key={j} cx={p[0] * VB_W} cy={p[1] * VB_H} r={j === 0 ? 8 : 5} className={j === 0 ? "vertex first" : "vertex"} />)}
                  </g>
                )}
              </svg>
            </div>
            <div className="row small muted zone-help">
              {draft
                ? `Click to add points (${draft.points.length}). Click the first point or double-click to finish. Esc cancels.`
                : "Pick a tool on the right, then click around the area. Click a zone to select it, drag its corners to adjust, Delete removes it."}
            </div>
          </div>

          <aside className="zone-side">
            <section>
              <h3>Draw</h3>
              <div className="row">
                <button className={`ghost ${draft?.type === "exclude" ? "on" : ""}`} onClick={() => { setSelected(null); setDraft({ type: "exclude", points: [] }); }}>
                  <span className="swatch exclude" /> Mask out area
                </button>
                <button className={`ghost ${draft?.type === "include" ? "on" : ""}`} onClick={() => { setSelected(null); setDraft({ type: "include", points: [] }); }}>
                  <span className="swatch include" /> Detect only in area
                </button>
                <button className={`ghost ${draft?.type === "area" ? "on" : ""}`} onClick={() => { setSelected(null); setDraft({ type: "area", points: [] }); }}>
                  <span className="swatch area" /> Name a place
                </button>
              </div>
              <p className="muted small">
                <strong>Mask out</strong> = ignore that area (e.g. the highway). <strong>Detect only in</strong> = if any exist, everything
                outside them is ignored. <strong>Name a place</strong> = filters nothing; each sighting records which named places it
                walked into ("Bathroom 2", "Exit door"), used in synopses, search and Ask.
              </p>
            </section>

            <section>
              <h3>Zones</h3>
              {zones.length === 0 && <p className="muted small">None: the whole frame is watched.</p>}
              {zones.map((z, i) => (
                <div key={i} className={`zone-row ${selected === i ? "selected" : ""}`} onClick={() => setSelected(i)}>
                  <span className={`swatch ${z.type}`} />
                  <input value={z.name} onChange={(e) => setZone(i, { name: e.target.value })} />
                  <select value={z.type} onChange={(e) => setZone(i, { type: e.target.value as Zone["type"] })}>
                    <option value="exclude">Mask out</option>
                    <option value="include">Detect only</option>
                    <option value="area">Named place</option>
                  </select>
                  <button className="ghost small" onClick={(e) => { e.stopPropagation(); setZones(zones.filter((_, j) => j !== i)); setSelected(null); }} aria-label="Delete zone">✕</button>
                </div>
              ))}
            </section>

            <section>
              <h3>Effect</h3>
              <label className="row small"><input type="checkbox" checked={showDets} onChange={(e) => setShowDets(e.target.checked)} /> Show last 24 h of detections ({dets.length})</label>
              <div className="legend small muted">
                <span><i className="dot-sw kept" /> kept</span>
                <span><i className="dot-sw masked" /> ignored</span>
              </div>
              <p className="small">
                <strong>{ignored}</strong> of {classified.length} recent detection points would be ignored.
                {preview && <> <strong>{preview.mask}</strong> past event{preview.mask === 1 ? "" : "s"} will be hidden{preview.restore ? <>, <strong>{preview.restore}</strong> restored</> : null}.</>}
              </p>
              <p className="muted small">Hidden events get the status "Masked". Recordings aren't touched, and removing a mask brings them back.</p>
            </section>

            <div className="row">
              <button onClick={save} disabled={saving || Boolean(draft)}>{saving ? "Saving…" : "Save zones"}</button>
              <button className="ghost" onClick={() => setStillT(Date.now() / 1000 - 5)} title="Grab a fresh still">Refresh image</button>
            </div>
            {result && <p className="small">{result}</p>}
          </aside>
        </div>
      </div>
    </div>
  );
}
