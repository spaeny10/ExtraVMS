/**
 * Home: the user's dashboard over every server they can see. Dashboards are per user; admins publish shared
 * ones. Nothing is stored until "Save as…", so the generated Default is always a safe starting point.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Dashboard } from "@site/dashboard/Dashboard";
import { nextFree } from "@site/dashboard/grid";
import { WIDGET_DEFS } from "@site/dashboard/palette";
import { DashboardToolbar } from "@site/dashboard/Toolbar";
import { WidgetSettings } from "@site/dashboard/WidgetSettings";
import { emptyDashboard, newWidgetId, type AnyWidget, type CameraGroup, type DashboardConfig, type DashboardList, type DashboardWidgetType } from "@site/dashboard/types";
import { confirmDialog, promptDialog, toast } from "@site/ui";
import { api, type Fleet, type Me, type Org } from "./api";
import { makeHubSource } from "./hubSource";

type Current = { id: string | null; name: string; shared: boolean; canEdit: boolean; config: DashboardConfig };

export function HomePage({ org, me }: { org: Org; me: Me }) {
  const [fleet, setFleet] = useState<Fleet | null>(null);
  const [groups, setGroups] = useState<CameraGroup[]>([]);
  const [list, setList] = useState<DashboardList | null>(null);
  const [current, setCurrent] = useState<Current | null>(null);
  const [draft, setDraft] = useState<DashboardConfig>(emptyDashboard());
  const [editing, setEditing] = useState(false);
  const [settingsFor, setSettingsFor] = useState<AnyWidget | null>(null);
  const admin = me.user.is_super || org.role === "admin" || org.role === "owner";
  const lastKey = `dashLast:${org.id}`;

  // fleet snapshot (cameras, sites) and groups
  useEffect(() => {
    const load = () => api.fleet(org.id).then(setFleet).catch(() => {});
    load();
    const t = setInterval(load, 15000);
    api.groups(org.id).then(setGroups).catch(() => setGroups([]));
    return () => clearInterval(t);
  }, [org.id]);
  const source = useMemo(() => makeHubSource(org, fleet, groups), [org, fleet, groups]);

  const open = useCallback(async (id: string | null, l: DashboardList) => {
    if (!id) {
      setCurrent({ id: null, name: "Default", shared: false, canEdit: false, config: l.generated });
      setDraft(l.generated);
      return;
    }
    try {
      const d = await api.dashboard(org.id, id);
      setCurrent({ id: d.id, name: d.name, shared: d.shared, canEdit: d.can_edit, config: d.config });
      setDraft(d.config);
      try { localStorage.setItem(lastKey, d.id); } catch { /* private mode */ }
    } catch {
      setCurrent({ id: null, name: "Default", shared: false, canEdit: false, config: l.generated });
      setDraft(l.generated);
    }
  }, [org.id, lastKey]);

  const reloadList = useCallback(() => api.dashboards(org.id).then((l) => { setList(l); return l; }), [org.id]);
  useEffect(() => {
    reloadList().then((l) => {
      let last: string | null = null;
      try { last = localStorage.getItem(lastKey); } catch { /* ignore */ }
      const id = l.default_id ?? (last && l.dashboards.some((d) => d.id === last) ? last : null);
      open(id, l);
    }).catch((e) => toast.error(e));
  }, [reloadList, open, lastKey]);

  const dirty = !!current && JSON.stringify(draft) !== JSON.stringify(current.config);
  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;
  useEffect(() => {
    const on = (e: BeforeUnloadEvent) => { if (dirtyRef.current) { e.preventDefault(); } };
    addEventListener("beforeunload", on);
    return () => removeEventListener("beforeunload", on);
  }, []);
  useEffect(() => {
    const on = (e: KeyboardEvent) => { if (e.key === "Escape" && editing) setEditing(false); };
    addEventListener("keydown", on);
    return () => removeEventListener("keydown", on);
  }, [editing]);

  if (!list || !current) return <p className="muted">Loading…</p>;

  const select = async (id: string | null) => {
    if (dirty && !(await confirmDialog("Discard unsaved changes?", { confirmLabel: "Discard", danger: true }))) return;
    setEditing(false);
    open(id, list);
  };
  const addWidget = (type: DashboardWidgetType) => {
    const def = WIDGET_DEFS[type];
    const pos = nextFree(draft.widgets, def.w, def.h, draft.cols);
    const w = { id: newWidgetId(), type, x: pos.x, y: pos.y, w: def.w, h: def.h, props: { ...def.props } } as AnyWidget;
    setDraft({ ...draft, widgets: [...draft.widgets, w] });
    if (type === "camera" || type === "briefing") setSettingsFor(w);
  };
  const save = async () => {
    if (!current.id) return saveAs();
    try {
      const d = await api.updateDashboard(org.id, current.id, { config: draft });
      setCurrent({ ...current, config: d.config });
      toast.success("Saved");
    } catch (e) { toast.error(e); }
  };
  const saveAs = async () => {
    const name = await promptDialog("Save dashboard as", { label: "Name", initial: current.id ? `${current.name} copy` : "My dashboard", confirmLabel: "Save" });
    if (!name?.trim()) return;
    try {
      const d = await api.createDashboard(org.id, { name: name.trim(), config: draft });
      const l = await reloadList();
      await open(d.id, l);
      toast.success(`Saved "${d.name}"`);
    } catch (e) { toast.error(e); }
  };
  const rename = async () => {
    const name = await promptDialog("Rename dashboard", { label: "Name", initial: current.name, confirmLabel: "Rename" });
    if (!name?.trim() || !current.id) return;
    try { await api.updateDashboard(org.id, current.id, { name: name.trim() }); const l = await reloadList(); await open(current.id, l); } catch (e) { toast.error(e); }
  };
  const del = async () => {
    if (!current.id || !(await confirmDialog(`Delete "${current.name}"?`, { confirmLabel: "Delete", danger: true }))) return;
    try { await api.deleteDashboard(org.id, current.id); const l = await reloadList(); await open(l.default_id && l.default_id !== current.id ? l.default_id : null, l); } catch (e) { toast.error(e); }
  };
  const publish = async (shared: boolean) => {
    if (!current.id) return;
    const ok = await confirmDialog(shared ? `Publish "${current.name}" to everyone in ${org.name}?` : `Unpublish "${current.name}"?`,
      { message: shared ? "Members see it read-only and can save their own copies. It stops being your private dashboard." : "It becomes your private dashboard again.", confirmLabel: shared ? "Publish" : "Unpublish" });
    if (!ok) return;
    try { await api.updateDashboard(org.id, current.id, { shared }); const l = await reloadList(); await open(current.id, l); toast.success(shared ? "Published" : "Unpublished"); } catch (e) { toast.error(e); }
  };
  const setDefault = async () => {
    const id = list.default_id === current.id ? null : current.id;
    try { await api.setDefaultDashboard(org.id, id); setList({ ...list, default_id: id }); } catch (e) { toast.error(e); }
  };

  return (
    <div className="dash-page">
      <DashboardToolbar list={list.dashboards} currentId={current.id} currentShared={current.shared} dirty={dirty} editing={editing}
        canEdit={current.canEdit} canPublish={admin && !!current.id} isDefault={!!current.id && list.default_id === current.id}
        onSelect={select} onEditing={setEditing} onAdd={addWidget} onSave={save} onSaveAs={saveAs} onRename={rename} onDelete={del}
        onDiscard={() => setDraft(current.config)} onPublish={publish} onSetDefault={setDefault} />
      {fleet && fleetEmpty(fleet, org) && <p className="muted small">No servers are enrolled yet. Add one under Customer → Servers and its cameras will appear here.</p>}
      <Dashboard source={source} config={draft} editing={editing} onChange={setDraft} onEditWidget={setSettingsFor} />
      {settingsFor && (
        <WidgetSettings widget={settingsFor} source={source} onClose={() => setSettingsFor(null)}
          onSave={(props) => setDraft((d) => ({ ...d, widgets: d.widgets.map((w) => (w.id === settingsFor.id ? { ...w, props } as AnyWidget : w)) }))} />
      )}
    </div>
  );
}

const fleetEmpty = (f: Fleet, org: Org) => (f.orgs.find((o) => o.org.id === org.id)?.sites.length ?? 0) === 0;
