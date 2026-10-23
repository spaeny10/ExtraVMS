/**
 * Pure helpers for the Site's combined Live view (SiteLive.tsx): grid shape, tile labels and the per-site layout.
 * (Not named siteLive.ts: it would differ from SiteLive.tsx only in case, which Windows and tsc reject.)
 * Kept free of React and @site imports so they can be unit-tested on their own.
 *
 * Tile keys are `camKey(serverId, cameraId)` = "<server>/<camera>" (frontend/src/playback.ts), so two servers with
 * a camera called "cam1" never collide in the layout, the live budget or painted regions.
 *
 * Two kinds of layout: the viewer's working arrangement (what the grid shows now, kept in this browser per Site, as
 * before), and named layouts saved on the hub per user and Site (hub/hub/layouts.py; they span the Site's servers).
 * The Site's Timeline lists the same layouts: a layout carries its {visible, solo, order} too, and each page reads and
 * saves only its own fields (Live never sends solo; the hub keeps it), so the Timeline's fields never count as a change.
 * Picking a named layout loads it into the working arrangement; the picker marks it changed (isDirty) until saved.
 */

export type Quality = "sd" | "hd";
/** A tile's stream: the viewer's SD / HD, or "auto" (no quality stored: the tile picks, frontend LiveQuality.tsx). */
export type QualityMode = Quality | "auto";
/**
 * What the viewer arranged for one Site. `visible: null` = every camera (so cameras added later show up);
 * `order: null` = server order, then each server's camera order. Keys not in `order` follow in that default order.
 * `cols: null` = Auto (gridCols); `activity` = the "Latest activity" column shows. `quality` holds the viewer's own SD / HD
 * picks (a click on the tile or All SD / All HD, or what a saved layout holds); a camera without one is on Auto.
 * The names follow the Timeline's LayoutConfig {visible, solo, order}; a saved Live layout stores this shape.
 * The working arrangement is stored in localStorage under `siteLive.<siteId>`.
 */
export type LiveLayout = { visible: string[] | null; order: string[] | null; quality: Record<string, Quality>; cols: number | null; activity: boolean };
export const EMPTY_LAYOUT: LiveLayout = { visible: null, order: null, quality: {}, cols: null, activity: true };
export const layoutStorageKey = (siteId: string) => `siteLive.${siteId}`;
/** the named layout picked for a Site (0 = "All cameras") */
export const layoutIdStorageKey = (siteId: string) => `siteLive.${siteId}.layoutId`;
/** whether the activity column shows under "All cameras": one preference per viewer, every Site */
export const ACTIVITY_PREF_KEY = "siteLive.activity";
/** a layout's fixed grid widths (null = Auto) */
export const COLUMN_CHOICES = [1, 2, 3, 4] as const;

/** Same shape as the server's LiveView: a square-ish grid, never wider than 4. */
export const gridCols = (n: number) => Math.min(4, Math.ceil(Math.sqrt(Math.max(1, n))));

/** "Server · Camera" only when the Site has several servers; one server's name would just repeat on every tile. */
export const tileLabel = (serverName: string, cameraName: string, multiServer: boolean) => (multiServer ? `${serverName} · ${cameraName}` : cameraName);

/** Step through a list that wraps at both ends (phone swipe / strip across all servers' cameras). */
export const wrapIndex = (i: number, dir: number, n: number) => (n <= 0 ? 0 : (((i + dir) % n) + n) % n);

/** `all` in display order: the layout's order first (unknown keys dropped), then the rest as they came. */
export function arrange(all: string[], layout: LiveLayout): string[] {
  if (!layout.order?.length) return all;
  const known = new Set(all);
  const head = layout.order.filter((k) => known.has(k));
  const seen = new Set(head);
  return [...head, ...all.filter((k) => !seen.has(k))];
}

export function isVisible(layout: LiveLayout, key: string) {
  return layout.visible === null || layout.visible.includes(key);
}

/** The cameras the grid shows, in order. A layout's cameras the Site no longer has are dropped here. */
export function shownKeys(all: string[], layout: LiveLayout): string[] {
  return arrange(all, layout).filter((k) => isVisible(layout, k));
}

/** Columns for n tiles: the layout's fixed count, or Auto. */
export const layoutCols = (layout: LiveLayout, n: number) => layout.cols ?? gridCols(n);

export type LayoutAction =
  | { type: "toggle"; key: string; all: string[] }
  /** "auto" forgets the tile's pick */
  | { type: "quality"; key: string; quality: QualityMode }
  | { type: "allQuality"; keys: string[]; quality: QualityMode }
  /** move `key` to just before `before` (null = to the end), in the arranged order of `all` */
  | { type: "move"; key: string; before: string | null; all: string[] }
  /** a tile dropped on another takes its place (the Timeline's tile drag): from before it lands after, and vice versa */
  | { type: "drop"; key: string; onto: string; all: string[] }
  | { type: "cols"; cols: number | null }
  | { type: "activity"; on: boolean }
  /** replace the arrangement (a named layout picked, or reverted to) */
  | { type: "load"; layout: LiveLayout }
  | { type: "reset" };

function withQuality(cur: Record<string, Quality>, keys: string[], q: QualityMode): Record<string, Quality> {
  const next = { ...cur };
  for (const k of keys) {
    if (q === "auto") delete next[k];
    else next[k] = q;
  }
  return next;
}

/** A tile's stream under this layout: its stored pick, else Auto. */
export const tileQuality = (layout: LiveLayout, key: string): QualityMode => layout.quality[key] ?? "auto";

export function layoutReducer(layout: LiveLayout, a: LayoutAction): LiveLayout {
  switch (a.type) {
    case "toggle": {
      const cur = layout.visible ?? a.all;
      const next = cur.includes(a.key) ? cur.filter((k) => k !== a.key) : [...cur, a.key];
      // everything shown again = back to "all", so cameras added to the site later appear without a visit here
      return { ...layout, visible: a.all.every((k) => next.includes(k)) ? null : next };
    }
    case "quality":
      return { ...layout, quality: withQuality(layout.quality, [a.key], a.quality) };
    case "allQuality":
      return { ...layout, quality: withQuality(layout.quality, a.keys, a.quality) };
    case "move": {
      if (a.key === a.before) return layout;
      const rest = arrange(a.all, layout).filter((k) => k !== a.key);
      const at = a.before === null ? rest.length : rest.indexOf(a.before);
      if (at < 0) return layout;
      return { ...layout, order: [...rest.slice(0, at), a.key, ...rest.slice(at)] };
    }
    case "drop": {
      const cur = arrange(a.all, layout);
      const to = cur.indexOf(a.onto);
      if (a.key === a.onto || to < 0 || !cur.includes(a.key)) return layout;
      const rest = cur.filter((k) => k !== a.key);
      rest.splice(to, 0, a.key);
      return { ...layout, order: rest };
    }
    case "cols":
      return layout.cols === a.cols ? layout : { ...layout, cols: a.cols };
    case "activity":
      return layout.activity === a.on ? layout : { ...layout, activity: a.on };
    case "load":
      return a.layout;
    case "reset":
      return EMPTY_LAYOUT;
  }
}

/**
 * The config a named layout is saved with: what the grid shows now, spelled out. `visible` and `order` list the
 * Site's current cameras explicitly (never null), so a camera added to the Site later stays out of this layout (it
 * shows under "All cameras", and Cameras adds it to the layout); quality only for those cameras.
 */
export function toSavedConfig(all: string[], layout: LiveLayout): LiveLayout {
  const known = new Set(all);
  const quality = Object.fromEntries(Object.entries(layout.quality).filter(([k]) => known.has(k)));
  return { visible: shownKeys(all, layout), order: arrange(all, layout), quality, cols: layout.cols, activity: layout.activity };
}

/**
 * What the viewer sees under a layout, as a comparable string: the shown cameras in order, their streams, the
 * columns and (unless `withActivity` is false) the activity column. The order of hidden cameras doesn't show, so it
 * doesn't count; neither do cameras the Site no longer has.
 */
function signature(all: string[], layout: LiveLayout, withActivity: boolean) {
  const shown = shownKeys(all, layout);
  return JSON.stringify({ shown, quality: shown.map((k) => tileQuality(layout, k)), cols: layout.cols, activity: withActivity ? layout.activity : null });
}

/**
 * Does the working arrangement differ from the picked layout (`saved`)? Under "All cameras" (`saved` = EMPTY_LAYOUT,
 * `withActivity` false) the activity column is left out: there it is the viewer's standing preference, not a change.
 */
export function isDirty(all: string[], current: LiveLayout, saved: LiveLayout, withActivity = true): boolean {
  return signature(all, current, withActivity) !== signature(all, saved, withActivity);
}

/** Tolerant read of a layout (stored in this browser, or a named layout's config from the hub): anything malformed
 *  falls back to the default rather than breaking the page. */
export function coerceLayout(raw: unknown): LiveLayout {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return EMPTY_LAYOUT;
  const v = raw as Partial<Record<keyof LiveLayout, unknown>>;
  const strings = (x: unknown) => (Array.isArray(x) && x.every((k) => typeof k === "string") ? (x as string[]) : null);
  const quality: Record<string, Quality> = {};
  if (v.quality && typeof v.quality === "object") for (const [k, q] of Object.entries(v.quality)) if (q === "sd" || q === "hd") quality[k] = q;
  const cols = typeof v.cols === "number" && (COLUMN_CHOICES as readonly number[]).includes(v.cols) ? v.cols : null;
  return { visible: strings(v.visible), order: strings(v.order), quality, cols, activity: v.activity !== false };
}

export function parseLayout(raw: string | null): LiveLayout {
  if (!raw) return EMPTY_LAYOUT;
  try { return coerceLayout(JSON.parse(raw)); } catch { return EMPTY_LAYOUT; }
}

export function loadLayout(siteId: string): LiveLayout {
  try { return parseLayout(localStorage.getItem(layoutStorageKey(siteId))); } catch { return EMPTY_LAYOUT; }
}

export function saveLayout(siteId: string, layout: LiveLayout) {
  try {
    const empty = layout.visible === null && layout.order === null && Object.keys(layout.quality).length === 0 && layout.cols === null && layout.activity;
    if (empty) localStorage.removeItem(layoutStorageKey(siteId));
    else localStorage.setItem(layoutStorageKey(siteId), JSON.stringify(layout));
  } catch { /* private mode: the layout just isn't remembered */ }
}

export function loadLayoutId(siteId: string): number {
  try { const n = Number(localStorage.getItem(layoutIdStorageKey(siteId))); return Number.isInteger(n) && n > 0 ? n : 0; } catch { return 0; }
}

export function saveLayoutId(siteId: string, id: number) {
  try { if (id) localStorage.setItem(layoutIdStorageKey(siteId), String(id)); else localStorage.removeItem(layoutIdStorageKey(siteId)); } catch { /* private mode */ }
}

export function loadActivityPref(): boolean {
  try { return localStorage.getItem(ACTIVITY_PREF_KEY) !== "0"; } catch { return true; }
}

export function saveActivityPref(on: boolean) {
  try { if (on) localStorage.removeItem(ACTIVITY_PREF_KEY); else localStorage.setItem(ACTIVITY_PREF_KEY, "0"); } catch { /* private mode */ }
}

/**
 * Cameras an embedding page wants in front (the SOC incident view: the cameras that saw the incident).
 * "first" puts them ahead of the rest, "only" shows just them.
 */
export type LiveFocus = { keys: string[]; mode: "first" | "only" };

/**
 * `ordered` with `focus` applied: focused keys in the focus's order (unknown ones dropped), then for "first" the rest.
 * A focus that names none of the Site's cameras (ids not loaded yet, a camera removed since) leaves the grid as it
 * was: an operator is better served by every camera than by an empty grid.
 */
export function applyFocus(ordered: string[], focus: LiveFocus | null | undefined): string[] {
  if (!focus?.keys.length) return ordered;
  const known = new Set(ordered);
  const head = [...new Set(focus.keys)].filter((k) => known.has(k));
  if (!head.length) return ordered;
  if (focus.mode === "only") return head;
  const seen = new Set(head);
  return [...head, ...ordered.filter((k) => !seen.has(k))];
}

// ---------------------------------------------------------------- the toolbar's View menu

export type StreamMode = "auto" | "sd" | "hd" | "mixed";
/** Every live tile on Auto, all SD, all HD, or a mix (no live tile: Auto). Drives the View menu's Stream buttons. */
export function streamMode(layout: LiveLayout, liveKeys: string[]): StreamMode {
  if (!liveKeys.length) return "auto";
  const first = tileQuality(layout, liveKeys[0]);
  return liveKeys.every((k) => tileQuality(layout, k) === first) ? first : "mixed";
}

export type ViewSummary = {
  stream: StreamMode;
  /** "Auto columns" / "3 columns" */
  cols: string;
  /** "Cameras 9/14", or "Cameras 14" when all show */
  cameras: string;
  /** some cameras are hidden: the View button says how many show ("View · 9/14") */
  filtered: string | null;
  /** the View button's tooltip: what the menu holds right now */
  title: string;
};

export function viewSummary(layout: LiveLayout, liveKeys: string[], shown: number, total: number): ViewSummary {
  const stream = streamMode(layout, liveKeys);
  const cols = layout.cols ? `${layout.cols} column${layout.cols > 1 ? "s" : ""}` : "Auto columns";
  const filtered = shown < total ? `${shown}/${total}` : null;
  const streamText = { auto: "Auto", sd: "All SD", hd: "All HD", mixed: "Mixed" }[stream];
  return {
    stream, cols, filtered,
    cameras: `Cameras ${filtered ?? total}`,
    title: `Stream ${streamText} · ${cols} · ${filtered ? `${filtered} cameras shown` : `all ${total} cameras`}`,
  };
}

// ---------------------------------------------------------------- keyboard

/**
 * The grid's arrow keys (docs/live-views-spec.md, Keyboard): the tile `key` moves the focus to from tile `i` of `n`
 * laid out `cols` wide, in visual order; null = nowhere (an edge, or not a move key). Down from a row above a short
 * last row lands on that row's last tile rather than nowhere. Home / End go to the first / last tile.
 */
export function gridMove(i: number, key: string, cols: number, n: number): number | null {
  if (n <= 0 || i < 0 || i >= n) return null;
  const c = Math.max(1, cols);
  switch (key) {
    case "ArrowLeft": return i > 0 ? i - 1 : null;
    case "ArrowRight": return i < n - 1 ? i + 1 : null;
    case "ArrowUp": return i >= c ? i - c : null;
    case "ArrowDown": return i + c < n ? i + c : Math.floor(i / c) < Math.floor((n - 1) / c) ? n - 1 : null;
    case "Home": return i === 0 ? null : 0;
    case "End": return i === n - 1 ? null : n - 1;
    default: return null;
  }
}
