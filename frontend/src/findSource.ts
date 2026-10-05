/**
 * Where Find (Find.tsx FindView) gets its data. The server UI uses this server (localFindSource); the hub's Site page
 * passes a source that spans every server of the Site (hub/ui/src/SiteFind.tsx), the way the Timeline takes
 * `apiFor`/`mediaFor`. Paging is cursor based so a multi-server source can keep a cursor per server.
 */
import type { ReactNode } from "react";
import { api, type EventQuery, type NvrEvent, type ParsedQuery, type SavedFindView, type SiteApi } from "./api";
import { DEFAULT_VIEW_KEY } from "./findViews";

/** An opaque "where the next page starts"; null = no more. */
export type FindCursor = unknown;
export type FindPage = { events: NvrEvent[]; next: FindCursor | null };
export type BrowseQuery = EventQuery & { sort?: "newest" | "priority" };
/** The event the viewer opens: the assistant's answers link by id only (this server's events). */
export type OpenedEvent = { id: number; e?: NvrEvent };

export type FindSource = {
  /** An event's camera as a filter value: FindView's `cameras[].id` (a camera id here, a lane key camKey(server,
   * camera) on the hub, whose camera list is named "Server · Camera"). */
  cameraKey: (e: NvrEvent) => string;
  /** Unique on the page (event ids are per server). */
  eventKey: (e: NvrEvent) => string;
  /** Newest-first order for live inserts (negative: a before b). */
  newer: (a: NvrEvent, b: NvrEvent) => number;
  events: (q: BrowseQuery, cursor: FindCursor | null, limit: number) => Promise<FindPage>;
  search: (text: string, q: EventQuery, cursor: FindCursor | null, limit: number) => Promise<FindPage>;
  /** "today", "last night"... read out of the text (time window, listing, footage phrase); null when unknown. */
  parseQuery: (text: string) => Promise<ParsedQuery | null>;
  views: {
    list: () => Promise<SavedFindView[]>;
    save: (views: SavedFindView[]) => Promise<SavedFindView[]>;
    /** may this viewer save / delete views (the starred default is always the viewer's own) */
    canEdit: boolean;
    /** localStorage key of the starred default view */
    defaultKey: string;
  };
  /** The client for an event's media (EventCard); default this server. */
  mediaFor?: (e: NvrEvent) => SiteApi;
  /** The event viewer; default the server UI's EventDetail for this server's event. */
  renderDetail?: (o: OpenedEvent, onClose: () => void) => ReactNode;
  /** Live updates beyond the `live` prop: new/updated events and removed ones (by eventKey). */
  subscribe?: (on: { event: (e: NvrEvent) => void; removed: (key: string) => void }) => () => void;
  /** Per-server features; off where they can't span servers yet. */
  features: { assistant: boolean; footage: boolean; identities: boolean; summary: boolean };
};

type LocalCursor = { before_id?: number; offset?: number };

/**
 * The next page's cursor for a single server's answer: newest-first pages by id (live inserts don't shift it),
 * priority order and search by offset. A page shorter than `limit` was the last one.
 */
export function nextLocalCursor(paging: "id" | "offset", page: { id: number }[], prev: LocalCursor | null, limit: number): LocalCursor | null {
  if (page.length < limit || !page.length) return null;
  return paging === "id" ? { before_id: page[page.length - 1].id } : { offset: (prev?.offset ?? 0) + page.length };
}

/** This server. */
export function localFindSource(): FindSource {
  return {
    cameraKey: (e) => e.camera_id,
    eventKey: (e) => String(e.id),
    newer: (a, b) => b.id - a.id,
    events: async (q, cursor, limit) => {
      const c = (cursor ?? {}) as LocalCursor;
      const r = await api.events({ ...q, limit, ...c });
      return { events: r, next: nextLocalCursor(q.sort === "priority" ? "offset" : "id", r, c, limit) };
    },
    search: async (text, q, cursor, limit) => {
      const c = (cursor ?? {}) as LocalCursor;
      const r = await api.search(text, { ...q, limit, offset: c.offset });
      return { events: r, next: nextLocalCursor("offset", r, c, limit) };
    },
    parseQuery: (text) => api.parseQuery(text).catch(() => null),
    views: {
      list: () => api.findViews().then((r) => r.views),
      save: (views) => api.saveFindViews(views).then((r) => r.views),
      canEdit: true,
      defaultKey: DEFAULT_VIEW_KEY,
    },
    features: { assistant: true, footage: true, identities: true, summary: true },
  };
}
