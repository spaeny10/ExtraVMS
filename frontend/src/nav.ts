import { createContext, useContext } from "react";

export type FocusMember = { id: number; cam: string; start: number; end: number };
/** id 0 = not an event: a moment in the footage (e.g. a footage search result). */
export type TimelineTarget = { id: number; camera_id: string; start_ts: number; end_ts: number | null; camera_class?: string;
  members?: FocusMember[] };
/** members: a cross-camera journey (all highlighted, their cameras shown, played from the first). */
export type TimelineFocus = { eventId: number; cam: string; start: number; end: number; label: string; nonce: number; members?: FocusMember[] };

type Nav = { openInTimeline: (e: TimelineTarget) => void };

/** App-level navigation that any view (e.g. the event viewer) can trigger. */
export const NavContext = createContext<Nav>({ openInTimeline: () => {} });
export const useNav = () => useContext(NavContext);

/** #timeline?cam=cam1&event=123[&journey=1]  or  #timeline?cam=cam1&t=1790270080 (a moment) */
export function parseTimelineHash(hash: string): { cam: string | null; event: number | null; journey: boolean; t: number | null } | null {
  if (!hash.startsWith("#timeline")) return null;
  const q = new URLSearchParams(hash.split("?")[1] ?? "");
  const ev = Number(q.get("event"));
  const t = Number(q.get("t"));
  return { cam: q.get("cam"), event: Number.isFinite(ev) && ev > 0 ? ev : null, journey: q.get("journey") === "1",
    t: Number.isFinite(t) && t > 0 ? t : null };
}

export const timelineHash = (cam: string, eventId: number, journey = false, t?: number) =>
  eventId ? `#timeline?cam=${encodeURIComponent(cam)}&event=${eventId}${journey ? "&journey=1" : ""}`
    : `#timeline?cam=${encodeURIComponent(cam)}&t=${Math.round(t ?? 0)}`;
