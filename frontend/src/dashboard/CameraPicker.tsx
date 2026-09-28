import type { DashboardSource } from "./source";
import type { CameraRef } from "./types";

/** Pick one camera (site + camera selects) or several (checkboxes grouped by site) from everything the source can see. */
export function CameraPicker({ source, value, onChange, single }: {
  source: DashboardSource; value: CameraRef[]; onChange: (v: CameraRef[]) => void; single?: boolean;
}) {
  const cams = source.cameras();
  const sites = source.sites();
  if (single) {
    const cur = value[0] ?? { site: "", camera: "" };
    const inSite = cams.filter((c) => c.site === cur.site);
    return (
      <div className="row">
        <label className="field"><span>Site</span>
          <select value={cur.site} onChange={(e) => onChange([{ site: e.target.value, camera: cams.find((c) => c.site === e.target.value)?.id ?? "" }])}>
            <option value="">— choose —</option>
            {sites.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
          </select></label>
        <label className="field"><span>Camera</span>
          <select value={cur.camera} disabled={!cur.site} onChange={(e) => onChange([{ site: cur.site, camera: e.target.value }])}>
            <option value="">— choose —</option>
            {inSite.map((c) => <option key={c.id} value={c.id}>{c.name}{c.online ? "" : " (offline)"}</option>)}
          </select></label>
      </div>
    );
  }
  const has = (site: string, id: string) => value.some((v) => v.site === site && v.camera === id);
  const toggle = (site: string, id: string, on: boolean) =>
    onChange(on ? [...value, { site, camera: id }] : value.filter((v) => !(v.site === site && v.camera === id)));
  return (
    <div className="cam-picker">
      {sites.map((s) => {
        const list = cams.filter((c) => c.site === s.id);
        const all = list.length > 0 && list.every((c) => has(s.id, c.id));
        return (
          <div key={s.id} className="cam-picker-site">
            <label className="row small"><input type="checkbox" checked={all} onChange={(e) => {
              const rest = value.filter((v) => v.site !== s.id);
              onChange(e.target.checked ? [...rest, ...list.map((c) => ({ site: s.id, camera: c.id }))] : rest);
            }} /> <strong>{s.name}</strong>{s.online ? "" : <span className="muted"> · offline</span>}</label>
            <div className="cam-picker-cams">
              {list.map((c) => <label key={c.id} className="row small"><input type="checkbox" checked={has(s.id, c.id)} onChange={(e) => toggle(s.id, c.id, e.target.checked)} /> {c.name}</label>)}
            </div>
          </div>
        );
      })}
      {sites.length === 0 && <div className="muted small">No sites yet.</div>}
    </div>
  );
}
