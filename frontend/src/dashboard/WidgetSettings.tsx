import { useState } from "react";
import { Icon } from "../ui";
import { CameraPicker } from "./CameraPicker";
import { WIDGET_DEFS } from "./palette";
import type { DashboardSource } from "./source";
import type { AnyWidget, EventsProps, WidgetProps } from "./types";

const KINDS = ["offline", "camera_down", "disk", "clock", "event_high", "event_policy", "event_watched"];

/** Per-widget settings in a small modal; Save hands the new props back. */
export function WidgetSettings({ widget, source, onSave, onClose }: {
  widget: AnyWidget; source: DashboardSource; onSave: (props: AnyWidget["props"]) => void; onClose: () => void;
}) {
  const [props, setProps] = useState<AnyWidget["props"]>(widget.props);
  const labels = source.extras?.kindLabels ?? {};
  const set = (p: object) => setProps((cur) => ({ ...cur, ...p }) as AnyWidget["props"]);
  let body: React.ReactNode;
  switch (widget.type) {
    case "camera": {
      const p = props as WidgetProps["camera"];
      body = (
        <>
          <CameraPicker source={source} single value={p.site ? [{ site: p.site, camera: p.camera }] : []} onChange={(v) => set({ site: v[0]?.site ?? "", camera: v[0]?.camera ?? "" })} />
          <label className="field"><span>Quality</span>
            <select value={p.quality ?? "sd"} onChange={(e) => set({ quality: e.target.value })}><option value="sd">SD (H.264, light)</option><option value="hd">HD (main stream)</option></select></label>
        </>
      );
      break;
    }
    case "events": {
      const p = props as EventsProps;
      const mode = p.group ? "group" : p.cameras?.length ? "cameras" : p.sites?.length ? "sites" : "all";
      const groups = source.groups();
      body = (
        <>
          <label className="field"><span>Show events from</span>
            <select value={mode} onChange={(e) => {
              const m = e.target.value;
              set({ group: m === "group" ? groups[0]?.id : undefined, cameras: m === "cameras" ? [] : undefined, sites: m === "sites" ? [] : undefined });
            }}>
              <option value="all">Every site I can see</option>
              <option value="sites">Chosen sites</option>
              <option value="cameras">Chosen cameras</option>
              <option value="group" disabled={!groups.length}>A camera group{groups.length ? "" : " (none defined)"}</option>
            </select></label>
          {mode === "group" && <label className="field"><span>Group</span>
            <select value={p.group ?? ""} onChange={(e) => set({ group: e.target.value })}>{groups.map((g) => <option key={g.id} value={g.id}>{g.name} · {g.members.length}</option>)}</select></label>}
          {mode === "sites" && <div className="cam-picker">{source.sites().map((s) => (
            <label key={s.id} className="row small"><input type="checkbox" checked={!!p.sites?.includes(s.id)} onChange={(e) => set({ sites: e.target.checked ? [...(p.sites ?? []), s.id] : (p.sites ?? []).filter((x) => x !== s.id) })} /> {s.name}</label>))}</div>}
          {mode === "cameras" && <CameraPicker source={source} value={p.cameras ?? []} onChange={(v) => set({ cameras: v })} />}
          <div className="row">
            <label className="field"><span>Types</span>
              <select value={p.classes?.length === 1 ? p.classes[0] : ""} onChange={(e) => set({ classes: e.target.value ? [e.target.value] : undefined })}>
                <option value="">People & vehicles</option><option value="person">People only</option><option value="vehicle">Vehicles only</option></select></label>
            <label className="field"><span>How many</span>
              <input type="number" min={5} max={100} value={p.limit ?? 20} onChange={(e) => set({ limit: Math.max(5, Math.min(100, +e.target.value || 20)) })} /></label>
          </div>
        </>
      );
      break;
    }
    case "briefing": {
      const p = props as WidgetProps["briefing"];
      body = (
        <>
          <label className="field"><span>Source</span>
            <select value={p.source} onChange={(e) => set(e.target.value === "site" ? { source: "site", site: source.sites()[0]?.id ?? "" } : { source: "digest", site: undefined })}>
              <option value="digest">Organisation digest (all sites)</option><option value="site">One site's daily briefing</option></select></label>
          {p.source === "site" && <label className="field"><span>Site</span>
            <select value={p.site} onChange={(e) => set({ site: e.target.value })}>{source.sites().map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}</select></label>}
        </>
      );
      break;
    }
    case "alerts": {
      const p = props as WidgetProps["alerts"];
      body = (
        <>
          <div className="field"><span>Kinds (none ticked = all)</span>
            <div className="cam-picker">{KINDS.map((k) => <label key={k} className="row small"><input type="checkbox" checked={!!p.kinds?.includes(k)} onChange={(e) => set({ kinds: e.target.checked ? [...(p.kinds ?? []), k] : (p.kinds ?? []).filter((x) => x !== k) })} /> {labels[k] ?? k}</label>)}</div></div>
          <label className="field"><span>How many</span><input type="number" min={5} max={100} value={p.limit ?? 20} onChange={(e) => set({ limit: Math.max(5, Math.min(100, +e.target.value || 20)) })} /></label>
        </>
      );
      break;
    }
    case "health": {
      const p = props as WidgetProps["health"];
      body = (
        <div className="field"><span>Sites (none ticked = all)</span>
          <div className="cam-picker">{source.sites().map((s) => <label key={s.id} className="row small"><input type="checkbox" checked={!!p.sites?.includes(s.id)} onChange={(e) => set({ sites: e.target.checked ? [...(p.sites ?? []), s.id] : (p.sites ?? []).filter((x) => x !== s.id) })} /> {s.name}</label>)}</div></div>
      );
      break;
    }
    case "ask": {
      const p = props as WidgetProps["ask"];
      body = <label className="field"><span>Placeholder</span><input value={p.placeholder ?? ""} maxLength={120} onChange={(e) => set({ placeholder: e.target.value || undefined })} /></label>;
      break;
    }
  }
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal dash-settings" onClick={(e) => e.stopPropagation()}>
        <header className="modal-head"><h2>{WIDGET_DEFS[widget.type].label}</h2><button className="ghost" onClick={onClose} aria-label="Close"><Icon name="x" /></button></header>
        <div className="dash-settings-body">
          <p className="muted small">{WIDGET_DEFS[widget.type].hint}</p>
          {body}
          <div className="row" style={{ justifyContent: "flex-end", marginTop: 12 }}>
            <button className="ghost" onClick={onClose}>Cancel</button>
            <button onClick={() => { onSave(props); onClose(); }}>Save</button>
          </div>
        </div>
      </div>
    </div>
  );
}
