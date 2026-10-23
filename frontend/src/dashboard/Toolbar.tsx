import { useEffect, useRef, useState } from "react";
import { Icon, menuKeys, useDismiss } from "../ui";
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

/**
 * Dashboard picker and edit controls, modeled on the Timeline's layout bar. The row keeps what is used every visit
 * (picker, Edit, Add widget, and Save / Discard while changed); Save as, Rename, Delete, Publish and "Open this first"
 * are in the ⋯ menu, so the row fits one line on a phone.
 */
export function DashboardToolbar(p: ToolbarProps) {
  return (
    <div className="dash-toolbar">
      <select value={p.currentId ?? ""} onChange={(e) => p.onSelect(e.target.value || null)} title="Dashboards: yours and the organization's shared ones">
        <option value="">Default (generated)</option>
        {p.list.map((d) => <option key={d.id} value={d.id}>{d.name}{d.shared ? " · shared" : ""}</option>)}
      </select>
      {p.dirty && <span className="dash-dirty" title="Unsaved changes">●</span>}
      {p.isDefault && <span className="muted small" title="This dashboard opens first (change it in the ⋯ menu)">★</span>}
      <button className={`small ${p.editing ? "" : "ghost"}`} onClick={() => p.onEditing(!p.editing)}>{p.editing ? "Done" : "Edit"}</button>
      {p.editing && (
        <select value="" onChange={(e) => { if (e.target.value) p.onAdd(e.target.value as DashboardWidgetType); }} title="Add a widget">
          <option value="">＋ Add widget…</option>
          {WIDGET_LIST.map((d) => <option key={d.type} value={d.type}>{d.label}</option>)}
        </select>
      )}
      {p.dirty && p.currentId && p.canEdit && <button className="small" onClick={p.onSave}>Save</button>}
      {p.dirty && <button className="ghost small" onClick={p.onDiscard}>Discard</button>}
      <MoreMenu {...p} />
    </div>
  );
}

/** The ⋯ menu: Save as, Rename, Delete, Publish / Unpublish and "Open this first", as far as they apply. */
function MoreMenu(p: ToolbarProps) {
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLSpanElement>(null);
  const btn = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  useDismiss(box, open, () => setOpen(false), btn);
  useEffect(() => { if (open) menu.current?.querySelector<HTMLElement>("[role^=menuitem]:not(:disabled)")?.focus(); }, [open]);
  const pick = (fn: () => void) => () => { setOpen(false); fn(); };
  const owned = !!p.currentId && p.canEdit && !p.dirty;
  const readOnly = p.currentShared && !p.canEdit;
  return (
    <span ref={box} className="dash-more">
      <button ref={btn} type="button" className="ghost small icon-act" aria-haspopup="menu" aria-expanded={open} aria-label="More dashboard actions"
        title="More: save as, rename, delete, publish, open first" onClick={() => setOpen((o) => !o)}>
        <Icon name="more" size={16} />
      </button>
      {open && (
        <div ref={menu} className="dash-more-menu" role="menu" aria-label="Dashboard" onKeyDown={menuKeys}>
          {readOnly && <div className="muted small dash-more-note">Shared · read-only. Save as… makes your own copy.</div>}
          <button type="button" role="menuitem" onClick={pick(p.onSaveAs)} title="Save what is on screen as a new dashboard of your own">Save as…</button>
          {owned && <button type="button" role="menuitem" onClick={pick(p.onRename)}>Rename…</button>}
          {owned && <button type="button" role="menuitem" onClick={pick(p.onDelete)}>Delete…</button>}
          {p.currentId && p.canPublish && !p.dirty && (
            <button type="button" role="menuitem" onClick={pick(() => p.onPublish(!p.currentShared))}
              title={p.currentShared ? "Make this your private dashboard again" : "Share with everyone in the organization (read-only for them)"}>
              {p.currentShared ? "Unpublish" : "Publish to org"}
            </button>
          )}
          {p.currentId && (
            <button type="button" role="menuitemcheckbox" aria-checked={p.isDefault} onClick={pick(p.onSetDefault)}
              title={p.isDefault ? "Stop opening this dashboard first" : "Open this dashboard first on every visit"}>
              Open this first
            </button>
          )}
        </div>
      )}
    </span>
  );
}
