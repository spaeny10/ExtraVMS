/**
 * Pure helpers for the Site's combined Timeline (SiteTimeline.tsx): its deep link, how a server's event becomes a
 * Timeline focus on lane keys, the first-visit visible lanes, and the hub-side (browser) store of named layouts.
 *
 * Link: /sites/<location_id>/timeline?server=<server_id>&cam=<camera_id>&event=<id>[&journey=1]
 *   or  /sites/<location_id>/timeline?server=<server_id>&cam=<camera_id>&t=<unix seconds>
 * `cam` is the camera's id on its server; the Timeline's lane key is camKey(server, cam). The Timeline's own
 * "Copy link" appends a server-UI style hash (#timeline?cam=…&event=…&server=…), which is accepted too.
 */
import type { Layout, LayoutConfig, NvrEvent } from "@site/api";
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

// ---- named layouts kept in this browser per Site (until the hub stores them: GET/PUT /api/locations/{id}/layouts)

export const layoutsStorageKey = (siteId: string) => `siteTimelineLayouts.${siteId}`;
type KV = Pick<Storage, "getItem" | "setItem">;

export function parseLayouts(raw: string | null): Layout[] {
  try {
    const v = JSON.parse(raw ?? "[]");
    return Array.isArray(v) ? v.filter((l) => l && typeof l.id === "number" && typeof l.name === "string" && l.config && typeof l.config === "object") : [];
  } catch {
    return [];
  }
}

/** A LayoutStore (Timeline.tsx) over one storage key. `kv` defaults to localStorage, read lazily. */
export function localLayoutStore(siteId: string, kv?: KV, now = () => Math.floor(Date.now() / 1000)): LayoutStore {
  const key = layoutsStorageKey(siteId);
  const storage = (): KV | null => { try { return kv ?? localStorage; } catch { return null; } };
  const read = () => { try { return parseLayouts(storage()?.getItem(key) ?? null); } catch { return []; } };
  const write = (ls: Layout[]) => {
    const s = storage();
    if (!s) throw new Error("Layouts can't be saved in this browser");
    s.setItem(key, JSON.stringify(ls));
  };
  const sorted = (ls: Layout[]) => [...ls].sort((a, b) => a.name.localeCompare(b.name));
  return {
    list: async () => sorted(read()),
    create: async ({ name, config }) => {
      const ls = read();
      const t = now();
      const l: Layout = { id: ls.reduce((m, x) => Math.max(m, x.id), 0) + 1, name, config, created_at: t, updated_at: t };
      write([...ls, l]);
      return l;
    },
    update: async (id, { name, config }) => {
      const ls = read();
      const old = ls.find((x) => x.id === id);
      if (!old) throw new Error("Layout not found");
      const l: Layout = { ...old, name, config, updated_at: now() };
      write(ls.map((x) => (x.id === id ? l : x)));
      return l;
    },
    remove: async (id) => { write(read().filter((x) => x.id !== id)); return { ok: true }; },
  };
}
