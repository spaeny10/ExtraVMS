/** Find views: built-in presets per role, saved views, the default view, URL (de)serialisation and the
 * client-side filter check for live events. Pure functions (no DOM) so they are unit-tested in findViews.test.ts. */
import { UNUSUAL_MIN, type Camera, type NvrEvent, type SavedFindView } from "./api";

export type FindFlag = "rule" | "ppe" | "unusual" | "watched" | "multicam" | "locked" | "corrected" | "false_alarm";
export const FLAGS: { id: FindFlag; label: string; title: string }[] = [
  { id: "rule", label: "🚫 Violations", title: "Breaks a site rule (towing, entry, PPE)" },
  { id: "ppe", label: "🦺 PPE", title: "PPE violations: no hard hat / hi-vis vest in a PPE zone" },
  { id: "unusual", label: "⚠ Unusual", title: "Unusual for this camera (time, place, how long it stayed)" },
  { id: "watched", label: "👁 Watched", title: "Matched someone on the watch list" },
  { id: "multicam", label: "🔗 Multi-camera", title: "Same person seen on more than one camera" },
  { id: "locked", label: "🔒 Locked", title: "Footage locked: kept regardless of retention" },
  { id: "corrected", label: "Corrected", title: "Synopsis corrected by an operator" },
  { id: "false_alarm", label: "False alarms", title: "Marked as a false alarm" },
];
const FLAG_IDS = new Set<string>(FLAGS.map((f) => f.id));

export type Priority = "" | "low" | "medium" | "high";
export const PRIORITIES: [Priority, string][] = [["", "Any priority"], ["low", "Low+"], ["medium", "Medium+"], ["high", "High"]];
const RANK: Record<string, number> = { none: 0, low: 1, medium: 2, high: 3 };

export type FindFilters = {
  camera: string;
  label: string;          // "" | person | vehicle
  hours: number;          // 0 = any time
  day: string;            // yyyy-mm-dd: that day only (overrides hours)
  minYolo: number;
  status: string;         // "" = everything
  priority: Priority;
  flags: FindFlag[];
  place: string;          // named area (zone type "area")
  sort: "newest" | "priority";
  attention: boolean;     // priority medium+ OR a broken rule OR unusual OR watched
};
export type FindMode = "events" | "grouped";
export type FindView = {
  id: string; name: string; icon: string; title?: string;
  filters: Partial<FindFilters>; mode: FindMode; builtin: boolean;
  suggestions?: string[];
  focusSearch?: boolean;
};

export const DEFAULT_FILTERS: FindFilters = {
  camera: "", label: "", hours: 24, day: "", minYolo: 0, status: "verified", priority: "", flags: [], place: "", sort: "newest", attention: false,
};

export const SUGGESTIONS = [
  "What happened overnight?",
  "Anything unusual today?",
  "Did anyone go outside today?",
  "How many people came through the East Door today?",
  "When was the last vehicle in the side yard?",
  "Was any camera offline in the last 24 hours?",
];

export const PRESETS: FindView[] = [
  { id: "attention", name: "Attention", icon: "⚑", builtin: true, mode: "events",
    title: "Operators: what needs a look in the last 24 h (priority medium+, violations, unusual, watched), most important first",
    filters: { attention: true, hours: 24, sort: "priority" },
    suggestions: ["What happened overnight?", "Anything unusual today?", "Was any camera offline in the last 24 hours?"] },
  { id: "compliance", name: "Compliance", icon: "🦺", builtin: true, mode: "events",
    title: "Site managers: site-rule and PPE violations over the last 7 days, with counts by rule, zone, camera and day",
    filters: { flags: ["rule"], hours: 168, sort: "newest" },
    suggestions: ["Who had PPE violations this week?", "Which trailers were moved this week?", "Anything unusual today?"] },
  { id: "investigate", name: "Investigate", icon: "🔎", builtin: true, mode: "events", focusSearch: true,
    title: "Investigators: any time, no flags; type what you are looking for",
    filters: { hours: 0, sort: "newest" },
    suggestions: ["When was the last vehicle in the side yard?", "Did anyone go outside today?", "How many people came through the East Door today?"] },
  { id: "everything", name: "Everything", icon: "☰", builtin: true, mode: "events",
    title: "Every verified event in the last 24 h, newest first",
    filters: {}, suggestions: SUGGESTIONS },
];

export const DEFAULT_VIEW_KEY = "find.defaultView";
export const FALLBACK_VIEW = "attention";

/** The view's full filters; minYolo comes from the viewer's own slider setting unless the view sets one. */
export function resolveFilters(view: FindView | undefined, storedMinYolo = 0): FindFilters {
  return { ...DEFAULT_FILTERS, minYolo: storedMinYolo, ...(view?.filters ?? {}), flags: [...(view?.filters.flags ?? [])] };
}

/** Which view opens when the URL names none: the starred one if it still exists, else Attention. */
export function defaultViewId(views: FindView[], stored: string | null | undefined): string {
  return stored && views.some((v) => v.id === stored) ? stored : FALLBACK_VIEW;
}

export function sameFilters(a: FindFilters, b: FindFilters): boolean {
  return (Object.keys(DEFAULT_FILTERS) as (keyof FindFilters)[]).every((k) =>
    k === "flags" ? [...a.flags].sort().join() === [...b.flags].sort().join() : a[k] === b[k]);
}

/** A saved view from the API, made safe to use (unknown flags dropped, filters typed). */
export function fromSaved(v: SavedFindView): FindView {
  const f = (v.filters ?? {}) as Partial<FindFilters>;
  const filters: Partial<FindFilters> = {};
  for (const k of Object.keys(DEFAULT_FILTERS) as (keyof FindFilters)[]) {
    if (f[k] === undefined) continue;
    if (k === "flags") filters.flags = (Array.isArray(f.flags) ? f.flags : []).filter((x) => FLAG_IDS.has(x));
    else if (typeof f[k] === typeof DEFAULT_FILTERS[k]) (filters as Record<string, unknown>)[k] = f[k];
  }
  return { id: v.id, name: v.name, icon: v.icon || "★", filters, mode: v.mode === "grouped" ? "grouped" : "events", builtin: false };
}

export function toSaved(id: string, name: string, f: FindFilters, mode: FindMode): SavedFindView {
  const { minYolo: _, ...rest } = f;  // the confidence slider stays the viewer's own setting
  void _;
  return { id, name, icon: "★", filters: { ...rest, flags: [...f.flags] }, mode };
}

// ---- URL state: ?view=attention&cam=cam3&hours=24&flags=ppe,unusual&priority=medium&place=Yard&sort=priority&q=…
// Only what differs from the view's own filters is written, so "?view=compliance" stays short. The page's path is
// never touched (the hub serves it under /s/<site>/), only the query string.

export type UrlState = { view?: string; patch: Partial<FindFilters>; q: string; mode?: FindMode };

export function buildQuery(viewId: string, f: FindFilters, base: FindFilters, q: string, mode: FindMode, viewMode: FindMode = "events"): string {
  const p = new URLSearchParams();
  p.set("view", viewId);
  if (f.camera !== base.camera) p.set("cam", f.camera);
  if (f.label !== base.label) p.set("type", f.label);
  if (f.day !== base.day) p.set("day", f.day);
  if (f.hours !== base.hours) p.set("hours", String(f.hours));
  if (f.minYolo !== base.minYolo) p.set("yolo", String(f.minYolo));
  if (f.status !== base.status) p.set("status", f.status || "all");
  if (f.priority !== base.priority) p.set("priority", f.priority || "any");
  if ([...f.flags].sort().join() !== [...base.flags].sort().join()) p.set("flags", [...f.flags].sort().join(",") || "none");
  if (f.place !== base.place) p.set("place", f.place);
  if (f.sort !== base.sort) p.set("sort", f.sort);
  if (f.attention !== base.attention) p.set("attn", f.attention ? "1" : "0");
  if (q) p.set("q", q);
  if (mode !== viewMode) p.set("mode", mode);
  return p.toString();
}

export function parseQuery(search: string): UrlState {
  const p = new URLSearchParams(search.startsWith("?") ? search.slice(1) : search);
  const patch: Partial<FindFilters> = {};
  const num = (v: string | null) => (v !== null && v !== "" && Number.isFinite(Number(v)) ? Number(v) : undefined);
  if (p.has("cam")) patch.camera = p.get("cam") ?? "";
  if (p.has("type")) { const t = p.get("type") ?? ""; if (t === "" || t === "person" || t === "vehicle") patch.label = t; }
  if (p.has("day")) { const d = p.get("day") ?? ""; if (d === "" || /^\d{4}-\d{2}-\d{2}$/.test(d)) patch.day = d; }
  const h = num(p.get("hours")); if (h !== undefined && h >= 0) patch.hours = h;
  const y = num(p.get("yolo")); if (y !== undefined && y >= 0 && y <= 1) patch.minYolo = y;
  if (p.has("status")) { const st = p.get("status") ?? ""; patch.status = st === "all" ? "" : st; }
  if (p.has("priority")) { const pr = p.get("priority"); if (pr === "any") patch.priority = ""; else if (pr === "low" || pr === "medium" || pr === "high") patch.priority = pr; }
  if (p.has("flags")) patch.flags = (p.get("flags") ?? "").split(",").filter((x): x is FindFlag => FLAG_IDS.has(x));
  if (p.has("place")) patch.place = p.get("place") ?? "";
  if (p.has("sort")) { const so = p.get("sort"); if (so === "newest" || so === "priority") patch.sort = so; }
  if (p.has("attn")) patch.attention = p.get("attn") === "1";
  const m = p.get("mode");
  return { view: p.get("view") || undefined, patch, q: p.get("q") ?? "", mode: m === "grouped" || m === "events" ? m : undefined };
}

/** [since, until] in unix seconds for the day picker or the hours chip. */
export function filterWindow(f: Pick<FindFilters, "day" | "hours">, now = Date.now()): [number | undefined, number | undefined] {
  if (f.day) {
    const a = new Date(f.day + "T00:00:00"), b = new Date(f.day + "T23:59:59");
    if (Number.isFinite(a.getTime())) return [a.getTime() / 1000, b.getTime() / 1000];
  }
  return [f.hours ? now / 1000 - f.hours * 3600 : undefined, undefined];
}

/** The API query for these filters (browse, search and summary all take it). */
export function eventQuery(f: FindFilters, now = Date.now()) {
  const [since, until] = filterWindow(f, now);
  return {
    camera: f.camera || undefined, status: f.status || undefined, label: f.label || undefined, min_yolo: f.minYolo || undefined,
    since, until, priority: f.priority || undefined, flags: f.flags.length ? f.flags : undefined, place: f.place || undefined,
    attention: f.attention || undefined,
  };
}

/** Same rules as the backend's event_filters, for a live event arriving over the WebSocket. */
export function matchesFilters(e: NvrEvent, f: FindFilters, now = Date.now()): boolean {
  const [since, until] = filterWindow(f, now);
  const unverified = e.status === "open" || e.status === "pending";
  const rank = RANK[e.priority ?? "none"] ?? 0;
  const unusual = (e.anomaly ?? 0) >= UNUSUAL_MIN;
  const flag: Record<FindFlag, boolean> = {
    rule: !!e.policy, ppe: e.policy?.kind === "ppe", unusual, watched: !!e.watched,
    multicam: (e.journey_cameras ?? 0) > 1, locked: !!e.locked, corrected: !!e.corrected_at, false_alarm: e.feedback?.verdict === "false_alarm",
  };
  return (!f.status || f.status.split(",").includes(e.status)) && (!f.camera || f.camera === e.camera_id) && (!f.label || f.label === e.camera_class)
    && (!f.minYolo || unverified || (e.yolo_conf ?? 0) >= f.minYolo)
    && (!since || e.start_ts >= since) && (!until || e.start_ts <= until)
    && (!f.priority || rank >= RANK[f.priority])
    && f.flags.every((x) => flag[x])
    && (!f.place || !!e.areas?.some((a) => a.name === f.place))
    && (!f.attention || rank >= 2 || !!e.policy || unusual || !!e.watched);
}

/** Named areas (zones of type "area") on the chosen camera, or on every camera. */
export function placeNames(cameras: Pick<Camera, "id" | "zones">[], camera = ""): string[] {
  const names = new Set<string>();
  for (const c of cameras) {
    if (camera && c.id !== camera) continue;
    for (const z of c.zones ?? []) if (z.type === "area" && z.name?.trim()) names.add(z.name.trim());
  }
  return [...names].sort((a, b) => a.localeCompare(b));
}

/** Browse list landmarks: consecutive events grouped by local hour, "Today 3 PM · 12 events". */
export function hourGroups<T extends { start_ts: number }>(events: T[], now = new Date()): { key: string; label: string; events: T[] }[] {
  const out: { key: string; label: string; events: T[] }[] = [];
  const yesterday = new Date(now); yesterday.setDate(now.getDate() - 1);
  for (const e of events) {
    const d = new Date(e.start_ts * 1000);
    const key = `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}-${d.getHours()}`;
    let g = out[out.length - 1];
    if (!g || g.key !== key) {
      const dayLabel = d.toDateString() === now.toDateString() ? "Today" : d.toDateString() === yesterday.toDateString() ? "Yesterday"
        : d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
      const hour = new Date(d.getFullYear(), d.getMonth(), d.getDate(), d.getHours()).toLocaleTimeString(undefined, { hour: "numeric" });
      g = { key, label: `${dayLabel} ${hour}`, events: [] };
      out.push(g);
    }
    g.events.push(e);
  }
  return out;
}
