import { useCallback, useEffect, useMemo, useState } from "react";
import { api, bandwidthText, fmtTime, type Camera, type HomeData, type NvrEvent, type SiteDashboard } from "./api";
import { Dashboard } from "./dashboard/Dashboard";
import { nextFree } from "./dashboard/grid";
import { LOCAL, makeLocalSource } from "./dashboard/localSource";
import { WIDGET_DEFS } from "./dashboard/palette";
import { DashboardToolbar } from "./dashboard/Toolbar";
import { WidgetSettings } from "./dashboard/WidgetSettings";
import { newWidgetId, type AnyWidget, type DashboardConfig, type DashboardWidgetType } from "./dashboard/types";
import { EventCard } from "./Events";
import { EventDetail } from "./EventDetail";
import { Skeleton, confirmDialog, promptDialog, toast } from "./ui";

const SEEN_KEY = "homeSeenAt";
const LAST_KEY = "homeDashLast";
const DEFAULT_KEY = "homeDashDefault";

function loadSeen(): number {
  try {
    const v = Number(localStorage.getItem(SEEN_KEY));
    return Number.isFinite(v) && v > 0 ? v : Date.now() / 1000 - 86400;
  } catch {
    return Date.now() / 1000 - 86400;
  }
}
const lsGet = (k: string) => { try { return localStorage.getItem(k); } catch { return null; } };
const lsSet = (k: string, v: string | null) => { try { if (v == null) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch { /* private mode */ } };

/** A generated starting point: every camera (up to 9), the events feed, today's briefing, site health and Ask. */
function generated(cameras: Camera[]): DashboardConfig {
  const cams = cameras.filter((c) => c.enabled).slice(0, 9);
  const widgets: AnyWidget[] = cams.map((c, i) => ({ id: `w_cam${i}`, type: "camera", x: (i % 3) * 4, y: Math.floor(i / 3) * 4, w: 4, h: 4, props: { site: LOCAL, camera: c.id, quality: "sd" } }));
  const n = Math.ceil(cams.length / 3) * 4;
  widgets.push(
    { id: "w_events", type: "events", x: 0, y: n, w: 8, h: 7, props: { limit: 20 } },
    { id: "w_brief", type: "briefing", x: 8, y: n, w: 4, h: 5, props: { source: "site", site: LOCAL } },
    { id: "w_health", type: "health", x: 8, y: n + 5, w: 4, h: 2, props: {} },
    { id: "w_ask", type: "ask", x: 8, y: n + 7, w: 4, h: 2, props: {} },
  );
  return { version: 1, cols: 12, rowH: 60, widgets };
}

type Current = { id: number | null; name: string; config: DashboardConfig };

/** The daily entry point: is everything recording, what needs attention since you last looked, and your
 * own arrangement of live tiles, events, the briefing and Ask (saved dashboards are shared on this site). */
export function HomeView({ cameras, port, onGo }: { cameras: Camera[]; port: number; onGo: (tab: "Live" | "Find" | "Settings") => void }) {
  const [seenAt] = useState(loadSeen);
  const [h, setH] = useState<HomeData | null>(null);
  const [err, setErr] = useState("");
  const [open, setOpen] = useState<number | null>(null);
  const name = (id: string) => cameras.find((c) => c.id === id)?.name ?? id;

  useEffect(() => {
    const load = () => api.home(seenAt).then((d) => { setH(d); setErr(""); }).catch((e) => setErr(String(e)));
    load();
    const t = setInterval(load, 30000);
    // what you've now seen becomes the new baseline for next time
    const mark = () => lsSet(SEEN_KEY, String(Date.now() / 1000));
    window.addEventListener("beforeunload", mark);
    return () => { clearInterval(t); window.removeEventListener("beforeunload", mark); mark(); };
  }, [seenAt]);

  // ---- dashboards (stored on the site, shared by everyone who uses it)
  const [list, setList] = useState<SiteDashboard[] | null>(null);
  const [current, setCurrent] = useState<Current | null>(null);
  const [draft, setDraft] = useState<DashboardConfig>(() => generated(cameras));
  const [editing, setEditing] = useState(false);
  const [settingsFor, setSettingsFor] = useState<AnyWidget | null>(null);
  const openDash = useCallback((id: number | null, l: SiteDashboard[]) => {
    const d = id == null ? null : l.find((x) => x.id === id);
    const cur: Current = d ? { id: d.id, name: d.name, config: d.config } : { id: null, name: "Default", config: generated(cameras) };
    setCurrent(cur); setDraft(cur.config);
    lsSet(LAST_KEY, d ? String(d.id) : null);
  }, [cameras]);
  const reload = useCallback(() => api.dashboards().then((l) => { setList(l); return l; }), []);
  useEffect(() => {
    reload().then((l) => {
      const pick = Number(lsGet(DEFAULT_KEY) ?? lsGet(LAST_KEY));
      openDash(l.some((d) => d.id === pick) ? pick : null, l);
    }).catch(() => { setList([]); openDash(null, []); });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reload]);
  // the generated default follows the camera list until it is saved
  useEffect(() => { if (current && current.id == null) { const g = generated(cameras); setCurrent({ ...current, config: g }); setDraft((d) => (JSON.stringify(d) === JSON.stringify(current.config) ? g : d)); } }, [cameras]); // eslint-disable-line react-hooks/exhaustive-deps

  const source = useMemo(() => makeLocalSource(cameras, h, port, {
    openEvent: (e) => setOpen(e.id),   // the event detail modal, as the old Home did
    ask: (q) => { try { sessionStorage.setItem("findAsk", q); } catch { /* ignore */ } onGo("Find"); },
  }), [cameras, h, port, onGo]);

  const dirty = !!current && JSON.stringify(draft) !== JSON.stringify(current.config);
  const addWidget = (type: DashboardWidgetType) => {
    const def = WIDGET_DEFS[type];
    const pos = nextFree(draft.widgets, def.w, def.h, draft.cols);
    const props = type === "briefing" ? { source: "site", site: LOCAL } : type === "camera" ? { ...def.props, site: LOCAL } : { ...def.props };
    const w = { id: newWidgetId(), type, x: pos.x, y: pos.y, w: def.w, h: def.h, props } as AnyWidget;
    setDraft({ ...draft, widgets: [...draft.widgets, w] });
    if (type === "camera") setSettingsFor(w);
  };
  const save = async () => {
    if (!current?.id) return saveAs();
    try { const d = await api.updateDashboard(current.id, { name: current.name, config: draft }); setCurrent({ id: d.id, name: d.name, config: d.config }); await reload(); toast.success("Saved"); }
    catch (e) { toast.error(e); }
  };
  const saveAs = async () => {
    const n = await promptDialog("Save dashboard as", { label: "Name", initial: current?.id ? `${current.name} copy` : "My dashboard", confirmLabel: "Save" });
    if (!n?.trim()) return;
    try { const d = await api.createDashboard({ name: n.trim(), config: draft }); const l = await reload(); openDash(d.id, l); toast.success(`Saved "${d.name}"`); }
    catch (e) { toast.error(e); }
  };
  const rename = async () => {
    if (!current?.id) return;
    const n = await promptDialog("Rename dashboard", { label: "Name", initial: current.name, confirmLabel: "Rename" });
    if (!n?.trim()) return;
    try { await api.updateDashboard(current.id, { name: n.trim(), config: current.config }); const l = await reload(); openDash(current.id, l); } catch (e) { toast.error(e); }
  };
  const del = async () => {
    if (!current?.id || !(await confirmDialog(`Delete "${current.name}"?`, { confirmLabel: "Delete", danger: true }))) return;
    try { await api.deleteDashboard(current.id); if (lsGet(DEFAULT_KEY) === String(current.id)) lsSet(DEFAULT_KEY, null); const l = await reload(); openDash(null, l); } catch (e) { toast.error(e); }
  };
  const select = async (id: string | null) => {
    if (dirty && !(await confirmDialog("Discard unsaved changes?", { confirmLabel: "Discard", danger: true }))) return;
    setEditing(false);
    openDash(id ? Number(id) : null, list ?? []);
  };
  const isDefault = !!current?.id && lsGet(DEFAULT_KEY) === String(current.id);
  const setDefault = () => { if (!current?.id) return; lsSet(DEFAULT_KEY, isDefault ? null : String(current.id)); setCurrent({ ...current }); };

  if (err && !h) return <div className="view error">{err}</div>;
  if (!h || !list || !current) return <div className="view home"><Skeleton lines={1} /><Skeleton lines={4} /></div>;
  const problems: string[] = [];
  // the link to the site is down: one line ("All cameras unreachable …", below) instead of one per camera
  const linkDown = (h.health_alerts ?? []).some((a) => a.kind === "site_link_down");
  for (const c of linkDown ? [] : h.cameras) {
    if (!c.stream_ready) problems.push(`${c.name} is not recording`);
    for (const p of c.health?.problems ?? []) problems.push(`${c.name}: ${p}`);
    if (c.stream_ready && c.metadata_last && h.now - c.metadata_last > 1800) problems.push(`${c.name}: no detections for ${Math.round((h.now - c.metadata_last) / 60)} min`);
  }
  if (h.retention_alert) problems.push("Retention can't hold the continuous window (disk full)");
  for (const a of h.health_alerts ?? []) problems.push(a.text);   // Hailo missing (YOLO on the CPU), verification stalled
  if (!h.yolo_ready && !(h.health_alerts ?? []).some((a) => a.kind === "detector_fallback")) problems.push("YOLO is still loading");
  if (!h.vlm_ready) problems.push("Qwen is still loading");
  const healthy = problems.length === 0;
  const usedPct = Math.round((1 - h.disk.free_gb / h.disk.total_gb) * 100);

  return (
    <div className="view home">
      <div className={`home-status ${healthy ? "ok" : "warn"}`}>
        <span className={`dot ${healthy ? "ok" : "bad"}`} />
        {healthy
          ? <span>All {h.cameras.length} cameras recording · {h.disk.free_gb.toLocaleString()} GB free ({usedPct}% used)
            {h.queues.verify + h.queues.synopsis > 0 && <span className="muted"> · {h.queues.verify} verifying, {h.queues.synopsis} awaiting Qwen</span>}</span>
          : <span>{problems.join(" · ")}</span>}
        {h.bandwidth && h.cameras.length > 0 && <span className="muted small" title="Video received from the cameras, averaged over 5 minutes">{bandwidthText(h.bandwidth)}</span>}
        <span className="spacer" />
        <button className="ghost small" onClick={() => onGo("Settings")}>Settings</button>
        <button className="ghost small" onClick={() => onGo("Live")}>Live view</button>
      </div>

      {h.attention.length > 0 && (
        <section>
          <h3>Needs attention <span className="muted small">since {fmtTime(h.since)} · {h.new_since} new sightings</span></h3>
          <div className="event-grid">{h.attention.map((e: NvrEvent) => <EventCard key={e.id} e={e} cameraName={name(e.camera_id)} onOpen={() => setOpen(e.id)} />)}</div>
        </section>
      )}

      <DashboardToolbar list={list.map((d) => ({ id: String(d.id), name: d.name, shared: false }))} currentId={current.id == null ? null : String(current.id)}
        currentShared={false} dirty={dirty} editing={editing} canEdit={current.id != null} canPublish={false} isDefault={isDefault}
        onSelect={select} onEditing={setEditing} onAdd={addWidget} onSave={save} onSaveAs={saveAs} onRename={rename} onDelete={del}
        onDiscard={() => setDraft(current.config)} onPublish={() => {}} onSetDefault={setDefault} />
      <Dashboard source={source} config={draft} editing={editing} onChange={setDraft} onEditWidget={setSettingsFor} />
      {settingsFor && (
        <WidgetSettings widget={settingsFor} source={source} onClose={() => setSettingsFor(null)}
          onSave={(props) => setDraft((d) => ({ ...d, widgets: d.widgets.map((w) => (w.id === settingsFor.id ? { ...w, props } as AnyWidget : w)) }))} />
      )}
      {open !== null && <EventDetail id={open} cameraName={name} onClose={() => setOpen(null)} />}
    </div>
  );
}
