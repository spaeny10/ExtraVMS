import { useEffect, useState, useSyncExternalStore } from "react";
import type { ExportJob } from "./api";
import { fmtClock } from "./playback";
import { toast } from "./ui";

/** A camera that can be picked: `key` is the lane key (camKey: "server/id" on the hub, the bare id on a server). */
export type RangeCamera = { key: string; name: string };
export type ExportRequest = {
  keys: string[]; start: number; end: number; quality: "original" | "sd"; audio: boolean; grid: boolean; lock: boolean;
  note: string; eventId?: number | null;
};
/** A started export: where its status and its zip are (one server's job, or the hub's for a Site across servers). */
export type ExportHandle = { status: () => Promise<ExportJob>; downloadUrl: () => string; label: string };
export type Exporter = (r: ExportRequest) => Promise<ExportHandle>;
export type Locker = (keys: string[], start: number, end: number, note: string) => Promise<void>;

export const EXPORT_MAX_S = 3600;   // backend exports.MAX_S
export const GRID_MAX_S = 1800;     // backend exports.GRID_MAX_S

/** Why the export can't be asked for as set (null = fine): the same limits the server checks. */
export function exportProblem(r: Pick<ExportRequest, "keys" | "start" | "end" | "grid">): string | null {
  if (!r.keys.length) return "Pick at least one camera.";
  if (r.end - r.start < 1) return "Select at least a second.";
  if (r.end - r.start > EXPORT_MAX_S) return `At most ${EXPORT_MAX_S / 60} minutes in one export.`;
  if (r.grid && r.end - r.start > GRID_MAX_S) return `The grid video is limited to ${GRID_MAX_S / 60} minutes.`;
  return null;
}

// ---------------------------------------------------------------- the exports of this session (they outlive the dialog)

type Running = { id: number; handle: ExportHandle; job: ExportJob | null; error: string | null };
let runs: Running[] = [];
let nextId = 1;
const subs = new Set<() => void>();
const emit = () => { runs = [...runs]; subs.forEach((f) => f()); };
export const exportsStore = {
  subscribe: (f: () => void) => { subs.add(f); return () => subs.delete(f); },
  get: () => runs,
};

function follow(handle: ExportHandle): Running {
  const run: Running = { id: nextId++, handle, job: null, error: null };
  runs = [run, ...runs].slice(0, 10);
  emit();
  const tick = async () => {
    try {
      run.job = await handle.status();
      run.error = null;
    } catch (e) {
      run.error = String(e);
    }
    emit();
    if (run.job?.state === "done") {
      toast.success(`Export ready: ${handle.label}`);
      return;
    }
    if (run.job?.state === "failed") {
      toast.error(`Export failed: ${run.job.error ?? ""}`);
      return;
    }
    window.setTimeout(tick, 1500);
  };
  void tick();
  return run;
}

const mb = (b: number) => `${(b / 1e6).toFixed(b < 1e7 ? 1 : 0)} MB`;

function ExportStatus({ run }: { run: Running }) {
  const j = run.job;
  if (!j) return <p className="muted small">{run.error ? `Can't reach the server: ${run.error}` : "Starting…"}</p>;
  if (j.state === "failed") return <p className="small error">Export failed: {j.error}</p>;
  if (j.state !== "done") {
    return (
      <div className="export-progress">
        <div className="bar"><span style={{ width: `${Math.round(j.progress * 100)}%` }} /></div>
        <span className="muted small">{j.step || "queued"} · {Math.round(j.progress * 100)}%</span>
      </div>
    );
  }
  return (
    <div className="export-done">
      <a className="button" href={run.handle.downloadUrl()} download={j.zip_name}>⬇ Download {j.zip_name} ({mb(j.zip_bytes)})</a>
      <span className="muted small">{j.files.filter((f) => f.kind !== "report").length} files, report and checksums · kept 7 days</span>
      {j.missing.length > 0 && <span className="small warn-text">Not included: {j.missing.map((m) => `${m.name} (${m.why})`).join(", ")}</span>}
    </div>
  );
}

/** The bar's chip for this session's exports: progress, then the download. */
export function ExportsChip() {
  const list = useSyncExternalStore(exportsStore.subscribe, exportsStore.get);
  const [open, setOpen] = useState(false);
  if (!list.length) return null;
  const busy = list.filter((r) => r.job?.state !== "done" && r.job?.state !== "failed").length;
  return (
    <div className="tl-menu">
      <button className={`ghost small tl-menu-btn ${busy ? "set" : ""}`} onClick={() => setOpen(!open)} title="Exports from this session">
        ⬇ Exports{busy ? ` · ${busy} running` : ""}
      </button>
      {open && (
        <div className="tl-pop right" role="dialog" aria-label="Exports">
          {list.map((r) => (
            <div key={r.id} className="tl-pop-sec export-row">
              <strong className="small">{r.handle.label}</strong>
              <ExportStatus run={r} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- the dialog

export function RangeDialog({ mode, start, end, cameras, ticked, eventId, exporter, locker, onClose }: {
  mode: "export" | "lock";
  start: number; end: number;
  cameras: RangeCamera[];
  /** the cameras ticked at first (export: the ones playing; lock: the lane dragged on) */
  ticked: string[];
  eventId?: number | null;
  exporter: Exporter;
  locker: Locker;
  onClose: () => void;
}) {
  const [keys, setKeys] = useState<string[]>(ticked.filter((k) => cameras.some((c) => c.key === k)));
  const [quality, setQuality] = useState<"original" | "sd">("original");
  const [audio, setAudio] = useState(true);
  const [grid, setGrid] = useState(false);
  const [lock, setLock] = useState(true);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [run, setRun] = useState<Running | null>(null);
  const live = useSyncExternalStore(exportsStore.subscribe, exportsStore.get).find((r) => r.id === run?.id) ?? run;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  const req: ExportRequest = { keys, start, end, quality, audio, grid, lock, note: note.trim(), eventId };
  const problem = mode === "export" ? exportProblem(req) : keys.length ? null : "Pick at least one camera.";
  const toggle = (k: string) => setKeys((cur) => (cur.includes(k) ? cur.filter((x) => x !== k) : [...cur, k]));
  const submit = async () => {
    if (problem) return;
    setBusy(true);
    try {
      if (mode === "lock") {
        await locker(keys, start, end, note.trim());
        toast.success(`Locked on ${keys.length} camera${keys.length > 1 ? "s" : ""} · kept until you unlock it`);
        onClose();
      } else {
        setRun(follow(await exporter(req)));
      }
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };
  const secs = Math.round(end - start);
  return (
    <div className="modal-backdrop dialog-backdrop" onClick={onClose}>
      <form className="dialog range-dialog" onClick={(e) => e.stopPropagation()} onSubmit={(e) => { e.preventDefault(); void submit(); }}>
        <h3>{mode === "export" ? "Export video" : "Lock footage"}</h3>
        <p className="muted small">{fmtClock(start)} → {fmtClock(end).slice(-8)} · {secs >= 120 ? `${Math.round(secs / 60)} min` : `${secs} s`}
          {eventId ? ` · event #${eventId}` : ""}</p>
        <fieldset className="range-cams" disabled={!!live}>
          <legend className="small">Cameras</legend>
          {cameras.map((c) => (
            <label key={c.key} className="row small"><input type="checkbox" checked={keys.includes(c.key)} onChange={() => toggle(c.key)} /> {c.name}</label>
          ))}
        </fieldset>
        {mode === "export" && (
          <fieldset className="range-opts" disabled={!!live}>
            <label className="row small">
              <select value={quality} onChange={(e) => setQuality(e.target.value as "original" | "sd")}>
                <option value="original">Original (as recorded)</option>
                <option value="sd">SD 720p (smaller)</option>
              </select>
            </label>
            <label className="row small"><input type="checkbox" checked={audio} onChange={(e) => setAudio(e.target.checked)} /> Audio</label>
            <label className="row small" title={end - start > GRID_MAX_S ? `Up to ${GRID_MAX_S / 60} minutes` : "All the cameras side by side in one video, with names and the time"}>
              <input type="checkbox" checked={grid} disabled={end - start > GRID_MAX_S} onChange={(e) => setGrid(e.target.checked)} /> Grid video
            </label>
            <label className="row small"><input type="checkbox" checked={lock} onChange={(e) => setLock(e.target.checked)} /> Also lock this footage</label>
          </fieldset>
        )}
        {!live && (
          <label className="field small"><span>{mode === "export" ? "Note for the report (optional)" : "Reason (optional)"}</span>
            <input value={note} onChange={(e) => setNote(e.target.value)} maxLength={300} /></label>
        )}
        {problem && <p className="small warn-text">{problem}</p>}
        {live && <ExportStatus run={live} />}
        <div className="row dialog-actions">
          <button type="button" className="ghost" onClick={onClose}>{live ? "Close" : "Cancel"}</button>
          {!live && <button type="submit" disabled={busy || !!problem}>{busy ? "Starting…" : mode === "export" ? "Export" : "Lock"}</button>}
        </div>
        {live && live.job?.state !== "done" && <p className="muted small">You can close this: the export carries on, and Exports in the bar has the download.</p>}
      </form>
    </div>
  );
}

/** The bar shown once a range is selected: Export or Lock, nothing else. */
export function RangeActions({ start, end, onExport, onLock, onClear }: {
  start: number; end: number; onExport: () => void; onLock: () => void; onClear: () => void;
}) {
  const secs = Math.round(end - start);
  return (
    <div className="lock-bar range-actions">
      <span>Selected {fmtClock(start)} → {fmtClock(end).slice(-8)} ({secs >= 120 ? `${Math.round(secs / 60)} min` : `${secs} s`})</span>
      <button onClick={onExport}>⬇ Export…</button>
      <button className="ghost" onClick={onLock}>🔒 Lock…</button>
      <button className="ghost small" onClick={onClear} aria-label="Clear the selection">✕</button>
    </div>
  );
}
