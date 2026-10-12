/**
 * Pure helpers for the Site's combined Timeline (SiteTimeline.tsx): its deep link, how a server's event becomes a
 * Timeline focus on lane keys, the first-visit visible lanes, and the named layouts (the hub's list shared with Live,
 * and the move of this browser's older Timeline layouts into it).
 *
 * Link: /sites/<location_id>/timeline?server=<server_id>&cam=<camera_id>&event=<id>[&journey=1]
 *   or  /sites/<location_id>/timeline?server=<server_id>&cam=<camera_id>&t=<unix seconds>
 * `cam` is the camera's id on its server; the Timeline's lane key is camKey(server, cam). The Timeline's own
 * "Copy link" appends a server-UI style hash (#timeline?cam=…&event=…&server=…), which is accepted too.
 */
import type { Layout, LayoutConfig, NvrEvent } from "@site/api";
import type { SiteLayout } from "./api";
import { parseTimelineHash, type FocusMember, type HashRegion, type TimelineFocus, type TimelineTarget } from "@site/nav";
import { camKey, splitKey } from "@site/playback";
import type { LayoutStore } from "@site/Timeline";

export type SiteTimelineQuery = {
  server: string | null; cam: string | null; event: number | null; t: number | null; journey: boolean; region: HashRegion | null;
};

export function siteTimelineHref(locationId: string, server: string, cam: string, eventId?: number | null, t?: number | null, journey = false): string {
  const q = new URLSearchParams({ server, cam });
  if (eventId) {
    q.set("event", String(eventId));
    if (journey) q.set("journey", "1");
  } else if (t) q.set("t", String(Math.round(t)));
  return `/sites/${encodeURIComponent(locationId)}/timeline?${q}`;
}

const posNum = (v: string | null): number | null => { const n = Number(v); return v && Number.isFinite(n) && n > 0 ? n : null; };
const REGION = /^([a-z0-9_]{1,32}):([A-Za-z0-9_-]{96})$/;

/** The deep link in a query string (with or without "?"), else in a "#timeline?…" hash; null when neither names a camera. */
export function parseSiteTimelineQuery(search: string | URLSearchParams, hash = ""): SiteTimelineQuery | null {
  const q = typeof search === "string" ? new URLSearchParams(search.startsWith("?") ? search.slice(1) : search) : search;
  if (q.get("cam")) {
    const m = REGION.exec(q.get("region") ?? "");
    return { server: q.get("server") || null, cam: q.get("cam"), event: posNum(q.get("event")), t: posNum(q.get("t")),
      journey: q.get("journey") === "1", region: m ? { cam: m[1], cells: m[2] } : null };
  }
  const h = parseTimelineHash(hash);
  if (h?.cam) return { server: h.server || null, cam: h.cam, event: h.event, t: h.t, journey: h.journey, region: h.region };
  return null;
}

/** The query string without the deep-link parameters (what "clear focus" leaves in the URL). */
export function withoutTimelineParams(search: string): string {
  const q = new URLSearchParams(search.startsWith("?") ? search.slice(1) : search);
  for (const k of ["server", "cam", "event", "t", "journey", "region"]) q.delete(k);
  const s = q.toString();
  return s ? `?${s}` : "";
}

/**
 * A server's event (or moment) as a focus on the combined Timeline, built as the server UI's App.tsx does but with
 * lane keys (camKey(server, camera)) and times moved onto the shared clock (shared = server time − offsetS).
 */
export function focusFor(server: string, e: TimelineTarget, offsetS = 0, nonce = Date.now()): TimelineFocus {
  const k = (cam: string) => camKey(server, cam);
  const m: FocusMember[] | null = e.members?.length
    ? e.members.map((x) => ({ id: x.id, cam: k(x.cam), start: x.start - offsetS, end: x.end - offsetS }))
    : null;
  return {
    eventId: e.id,
    cam: m ? m[0].cam : k(e.camera_id),
    start: m ? Math.min(...m.map((x) => x.start)) : e.start_ts - offsetS,
    end: m ? Math.max(...m.map((x) => x.end)) : (e.end_ts ?? e.start_ts) - offsetS,
    label: m ? `journey across ${new Set(m.map((x) => x.cam)).size} cameras` : e.camera_class ?? "event",
    nonce,
    members: m ?? undefined,
  };
}

/** A journey's sightings as focus members (server-local camera ids), as App.tsx does. */
/**
 * The inverse for "Open in Timeline" from an event viewer inside the combined Timeline: TimelineView hands the
 * opener lane keys (camKey(server, camera)), so the event's server is read from the key and the target is put back
 * into that server's camera ids. null when the key names no server (not a combined-Timeline lane).
 */
export function fromLaneKeys(e: TimelineTarget): { server: string; target: TimelineTarget } | null {
  const { server } = splitKey(e.members?.[0]?.cam ?? e.camera_id);
  if (!server) return null;
  const local = (k: string) => splitKey(k).id;
  return { server, target: { ...e, camera_id: local(e.camera_id), ...(e.members ? { members: e.members.map((m) => ({ ...m, cam: local(m.cam) })) } : {}) } };
}

export const journeyMembers = (events: Pick<NvrEvent, "id" | "camera_id" | "start_ts" | "end_ts">[]): FocusMember[] =>
  events.map((x) => ({ id: x.id, cam: x.camera_id, start: x.start_ts, end: x.end_ts ?? x.start_ts }));

/** A moment (footage search result, t= link) as a focus target, as App.tsx does. */
export const momentTarget = (cam: string, t: number): TimelineTarget => ({ id: 0, camera_id: cam, start_ts: t, end_ts: t + 5, camera_class: "moment" });

/** The Timeline's clock offset for a server: its clock skew, ignored up to 2 s as TimelineView does. */
export const effectiveOffset = (skew: number | null | undefined): number => (skew != null && Math.abs(skew) > 2 ? skew : 0);

export const MAX_DEFAULT_PER_SERVER = 8;
/** First-visit visible lanes: everything for a small Site (null = all); past `total` cameras, the first 8 of each server. */
export function defaultVisible(cams: { key: string; server: string }[], perServer = MAX_DEFAULT_PER_SERVER): string[] | null {
  if (cams.length <= perServer) return null;
  const n = new Map<string, number>();
  const out: string[] = [];
  for (const c of cams) {
    const i = n.get(c.server) ?? 0;
    n.set(c.server, i + 1);
    if (i < perServer) out.push(c.key);
  }
  return out.length === cams.length ? null : out;
}

/**
 * The lighter remote default: through the hub (the Site's uplink carries every tile), a Site with more than two
 * cameras starts on one camera — the deep link's, else the first shown lane — with the other lanes still listed.
 * null = keep the grid (direct/LAN, or a small Site).
 */
export function lightSolo(keys: string[], visible: string[] | null, focusKey: string | null, viaHub: boolean): string | null {
  if (!viaHub || keys.length <= 2) return null;
  if (focusKey && keys.includes(focusKey)) return focusKey;
  return (visible ?? keys).find((k) => keys.includes(k)) ?? null;
}

export type StoredLayoutConfig = { visible: string[] | null; solo: string | null; order: string[] | null };
/**
 * What to write to TimelineView's stored layout before it mounts. `stored` = the saved config (null = first visit),
 * `flag` = the camera an earlier hub visit soloed automatically (null = none). Returns the config to write
 * (undefined = leave it) and the new flag (undefined = leave, null = remove).
 * First visit: the per-server lane cap and, via the hub, the one-camera start (flagged, so it can be undone). Later
 * visits only touch an automatic solo the user kept: back to the grid once the Site is reached directly. Once the
 * user picks something else the flag is dropped, and the layout is theirs.
 */
export function initialTimelineLayout(o: {
  stored: StoredLayoutConfig | null; flag: string | null; cams: { key: string; server: string }[]; focusKey: string | null; viaHub: boolean;
}): { config?: StoredLayoutConfig; flag?: string | null } {
  if (!o.stored) {
    const visible = defaultVisible(o.cams);
    const solo = lightSolo(o.cams.map((c) => c.key), visible, o.focusKey, o.viaHub);
    if (!visible && !solo) return {};
    return { config: { visible, solo, order: null }, ...(solo ? { flag: solo } : {}) };
  }
  if (!o.flag) return {};
  if (o.stored.solo !== o.flag) return { flag: null };
  if (!o.viaHub) return { config: { ...o.stored, solo: null }, flag: null };
  return {};
}

// ---- named layouts: the hub's per-user, per-Site list (hub/hub/layouts.py), shared with the Site's Live grid

/** The hub calls a layout store needs (api.ts), passed in so these helpers stay testable without a hub. */
export type SiteLayoutsApi = {
  siteLayouts(id: string): Promise<SiteLayout[]>;
  createSiteLayout(id: string, b: { name: string; config: unknown }): Promise<SiteLayout>;
  updateSiteLayout(id: string, lid: number, b: { name: string; config: unknown }): Promise<SiteLayout>;
  deleteSiteLayout(id: string, lid: number): Promise<unknown>;
};

/**
 * The Timeline's own fields of a stored layout config, read tolerantly. The hub keeps Live's fields (quality, cols,
 * activity) beside them; the Timeline neither sees them (so they never count as a change here) nor sends them (the
 * hub merges an update into the stored config, so they are kept).
 */
export function timelineConfig(raw: unknown): LayoutConfig {
  const v = raw && typeof raw === "object" && !Array.isArray(raw) ? (raw as Record<string, unknown>) : {};
  const strings = (x: unknown) => (Array.isArray(x) && x.every((k) => typeof k === "string") ? (x as string[]) : null);
  return { visible: strings(v.visible), solo: typeof v.solo === "string" && v.solo ? v.solo : null, order: strings(v.order) };
}

/** A LayoutStore (Timeline.tsx) over the hub's list for one Site, seeing and sending only the Timeline's fields. */
export function hubLayoutStore(siteId: string, hub: SiteLayoutsApi): LayoutStore {
  const own = (l: SiteLayout): Layout => ({ ...l, config: timelineConfig(l.config) });
  return {
    list: async () => (await hub.siteLayouts(siteId)).map(own),
    create: async ({ name, config }) => own(await hub.createSiteLayout(siteId, { name, config: timelineConfig(config) })),
    update: async (id, { name, config }) => own(await hub.updateSiteLayout(siteId, id, { name, config: timelineConfig(config) })),
    remove: (id) => hub.deleteSiteLayout(siteId, id),
  };
}

// Before the hub stored them, the Timeline's named layouts were kept in this browser per Site under this key. A visit
// to the Site's Timeline moves them to the hub once (migrateLocalLayouts).
export const layoutsStorageKey = (siteId: string) => `siteTimelineLayouts.${siteId}`;
type KV = Pick<Storage, "getItem" | "setItem" | "removeItem">;
const NAME_MAX = 60;   // the hub's limit on a layout name

export function parseLayouts(raw: string | null): Layout[] {
  try {
    const v = JSON.parse(raw ?? "[]");
    return Array.isArray(v) ? v.filter((l) => l && typeof l.id === "number" && typeof l.name === "string" && l.config && typeof l.config === "object") : [];
  } catch {
    return [];
  }
}

/** `name`, or when the list has it already (names are unique ignoring case) "name (Timeline)", then "(Timeline 2)"… */
export function uniqueLayoutName(name: string, taken: ReadonlySet<string>): string {
  const base = name.trim() || "Timeline layout";
  const fit = (suffix: string) => base.slice(0, NAME_MAX - suffix.length).trimEnd() + suffix;
  for (let n = 0; ; n++) {
    const candidate = fit(n === 0 ? "" : n === 1 ? " (Timeline)" : ` (Timeline ${n})`);
    if (!taken.has(candidate.toLowerCase())) return candidate;
  }
}

/**
 * Move this browser's old Timeline layouts of a Site into the hub's shared list, once: each is created there (a name
 * the list has already gets " (Timeline)"), and the local copy is removed once it is. One the hub refused stays here
 * and is tried again on the next visit. The picked layout (`layoutIdKey`, TimelineView's timelineLayoutId) named a
 * local id: it is moved to the hub's id, or to "All cameras" (0) while its layout is still here. A hub that can't list
 * the layouts throws, and nothing is moved. Returns how many moved and how many are left.
 */
export async function migrateLocalLayouts(siteId: string, hub: SiteLayoutsApi, layoutIdKey: string, kv?: KV): Promise<{ moved: number; left: number }> {
  const key = layoutsStorageKey(siteId);
  let s: KV;
  let raw: string | null;
  try { s = kv ?? localStorage; raw = s.getItem(key); } catch { return { moved: 0, left: 0 }; }   // no storage: nothing kept here
  if (raw == null) return { moved: 0, left: 0 };
  const local = parseLayouts(raw).sort((a, b) => a.id - b.id);
  const taken = local.length ? new Set((await hub.siteLayouts(siteId)).map((l) => l.name.toLowerCase())) : new Set<string>();
  const left: Layout[] = [];
  const ids = new Map<number, number>();
  for (const l of local) {
    try {
      const made = await hub.createSiteLayout(siteId, { name: uniqueLayoutName(l.name, taken), config: timelineConfig(l.config) });
      taken.add(made.name.toLowerCase());
      ids.set(l.id, made.id);
    } catch {
      left.push(l);
    }
  }
  try {
    if (left.length) s.setItem(key, JSON.stringify(left));
    else s.removeItem(key);
    const picked = Number(s.getItem(layoutIdKey));
    if (picked && local.some((l) => l.id === picked)) s.setItem(layoutIdKey, String(ids.get(picked) ?? 0));
  } catch { /* storage gone mid-way: a later visit finds the copies and tries again */ }
  return { moved: ids.size, left: left.length };
}
