/**
 * Pure helpers for the Site's combined Live view (SiteLive.tsx): grid shape, tile labels and the per-site layout.
 * (Not named siteLive.ts: it would differ from SiteLive.tsx only in case, which Windows and tsc reject.)
 * Kept free of React and @site imports so they can be unit-tested on their own.
 *
 * Tile keys are `camKey(serverId, cameraId)` = "<server>/<camera>" (frontend/src/playback.ts), so two servers with
 * a camera called "cam1" never collide in the layout, the live budget or painted regions.
 */

export type Quality = "sd" | "hd";
/**
 * What the viewer arranged for one Site. `visible: null` = every camera (so cameras added later show up);
 * `order: null` = server order, then each server's camera order. Keys not in `order` follow in that default order.
 * Stored in localStorage under `siteLive.<siteId>` until the hub keeps layouts (GET/PUT /api/locations/{id}/layouts).
 */
export type LiveLayout = { visible: string[] | null; order: string[] | null; quality: Record<string, Quality> };
export const EMPTY_LAYOUT: LiveLayout = { visible: null, order: null, quality: {} };
export const layoutStorageKey = (siteId: string) => `siteLive.${siteId}`;

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

export type LayoutAction =
  | { type: "toggle"; key: string; all: string[] }
  | { type: "quality"; key: string; quality: Quality }
  | { type: "allQuality"; keys: string[]; quality: Quality }
  /** move `key` to just before `before` (null = to the end), in the arranged order of `all` */
  | { type: "move"; key: string; before: string | null; all: string[] }
  | { type: "reset" };

export function layoutReducer(layout: LiveLayout, a: LayoutAction): LiveLayout {
  switch (a.type) {
    case "toggle": {
      const cur = layout.visible ?? a.all;
      const next = cur.includes(a.key) ? cur.filter((k) => k !== a.key) : [...cur, a.key];
      // everything shown again = back to "all", so cameras added to the site later appear without a visit here
      return { ...layout, visible: a.all.every((k) => next.includes(k)) ? null : next };
    }
    case "quality":
      return { ...layout, quality: { ...layout.quality, [a.key]: a.quality } };
    case "allQuality":
      return { ...layout, quality: { ...layout.quality, ...Object.fromEntries(a.keys.map((k) => [k, a.quality])) } };
    case "move": {
      if (a.key === a.before) return layout;
      const rest = arrange(a.all, layout).filter((k) => k !== a.key);
      const at = a.before === null ? rest.length : rest.indexOf(a.before);
      if (at < 0) return layout;
      return { ...layout, order: [...rest.slice(0, at), a.key, ...rest.slice(at)] };
    }
    case "reset":
      return EMPTY_LAYOUT;
  }
}

/** Tolerant read of a stored layout: anything malformed falls back to the default rather than breaking the page. */
export function parseLayout(raw: string | null): LiveLayout {
  if (!raw) return EMPTY_LAYOUT;
  try {
    const v = JSON.parse(raw) as Partial<LiveLayout>;
    const strings = (x: unknown) => (Array.isArray(x) && x.every((k) => typeof k === "string") ? (x as string[]) : null);
    const quality: Record<string, Quality> = {};
    if (v.quality && typeof v.quality === "object") for (const [k, q] of Object.entries(v.quality)) if (q === "sd" || q === "hd") quality[k] = q;
    return { visible: strings(v.visible), order: strings(v.order), quality };
  } catch {
    return EMPTY_LAYOUT;
  }
}

export function loadLayout(siteId: string): LiveLayout {
  try { return parseLayout(localStorage.getItem(layoutStorageKey(siteId))); } catch { return EMPTY_LAYOUT; }
}

export function saveLayout(siteId: string, layout: LiveLayout) {
  try {
    const empty = layout.visible === null && layout.order === null && Object.keys(layout.quality).length === 0;
    if (empty) localStorage.removeItem(layoutStorageKey(siteId));
    else localStorage.setItem(layoutStorageKey(siteId), JSON.stringify(layout));
  } catch { /* private mode: the layout just isn't remembered */ }
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
