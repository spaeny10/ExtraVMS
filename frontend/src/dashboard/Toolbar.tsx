import { WIDGET_LIST } from "./palette";
import type { DashboardWidgetType } from "./types";

export type ToolbarProps = {
  list: { id: string; name: string; shared: boolean }[];
  currentId: string | null;         // null = the generated "Default"
  currentShared: boolean;
  dirty: boolean; editing: boolean; canEdit: boolean; canPublish: boolean; isDefault: boolean;
  onSelect: (id: string | null) => void;
  onEditing: (on: boolean) => void;
  onAdd: (type: DashboardWidgetType) => void;
  onSave: () => void; onSaveAs: () => void; onRename: () => void; onDelete: () => void; onDiscard: () => void;
  onPublish: (shared: boolean) => void; onSetDefault: () => void;
};

/** Dashboard picker and edit controls, modelled on the Timeline's layout bar. */
export function DashboardToolbar(p: ToolbarProps) {
  return (
    <div className="dash-toolbar">
      <select value={p.currentId ?? ""} onChange={(e) => p.onSelect(e.target.value || null)} title="Dashboards: yours and the organisation's shared ones">
        <option value="">Default (generated)</option>
        {p.list.map((d) => <option key={d.id} value={d.id}>{d.name}{d.shared ? " · shared" : ""}</option>)}
      </select>
      {p.dirty && <span className="dash-dirty" title="Unsaved changes">●</span>}
      {p.currentId && <button className={`ghost small ${p.isDefault ? "on" : ""}`} title={p.isDefault ? "This opens first (click to unset)" : "Open this dashboard first"} onClick={p.onSetDefault}>{p.isDefault ? "★" : "☆"}</button>}
      <button className={`small ${p.editing ? "" : "ghost"}`} onClick={() => p.onEditing(!p.editing)}>{p.editing ? "Done" : "Edit"}</button>
      {p.editing && (
        <select value="" onChange={(e) => { if (e.target.value) p.onAdd(e.target.value as DashboardWidgetType); }} title="Add a widget">
          <option value="">＋ Add widget…</option>
          {WIDGET_LIST.map((d) => <option key={d.type} value={d.type}>{d.label}</option>)}
        </select>
      )}
      {p.dirty && p.currentId && p.canEdit && <button className="small" onClick={p.onSave}>Save</button>}
      {(p.dirty || !p.currentId) && <button className="ghost small" onClick={p.onSaveAs} title="Save as a new dashboard of your own">Save as…</button>}
      {p.dirty && <button className="ghost small" onClick={p.onDiscard}>Discard</button>}
      {p.currentId && p.canEdit && !p.dirty && <button className="ghost small" onClick={p.onRename}>Rename</button>}
      {p.currentId && p.canEdit && !p.dirty && <button className="ghost small" onClick={p.onDelete}>Delete</button>}
      {p.currentId && p.canPublish && !p.dirty && (
        <button className="ghost small" onClick={() => p.onPublish(!p.currentShared)} title={p.currentShared ? "Make this your private dashboard again" : "Share with everyone in the organisation (read-only for them)"}>
          {p.currentShared ? "Unpublish" : "Publish to org"}
        </button>
      )}
      {p.currentShared && !p.canEdit && <span className="muted small">shared · read-only (Save as… for your own copy)</span>}
    </div>
  );
}
