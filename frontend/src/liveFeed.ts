/**
 * Live → "Latest activity" (LiveView here, Site Live on the hub through @site/liveFeed): which events show as cards.
 * A vehicle the camera reported shows only once YOLO has verified it. Parked cars and cameras whose own vehicle
 * detection fires on nothing opened an event a minute (2026-10-19), each a "Waiting for verification… Tracking" card
 * that vanished a minute later when it was rejected, so the column was full of cards that were never anything.
 * People (and every other label) still show the moment they open. The tiles' badge still lights "vehicle" while one is
 * open (the callers' activeOf reads the whole list): only the cards wait, and a muted line counts them.
 * The column's "Show [People] [Vehicles] [PPE violations]" pills are the Timeline's (classFilter.ts): they filter the
 * cards and the vehicles counted, never the tiles' badges, and are remembered per viewer apart from the Timeline's.
 */
import type { NvrEvent } from "./api";
import { type ClassFilter, DEFAULT_CLASS_FILTER, classPass, parseClassFilter } from "./classFilter";

export { type ClassFilter, DEFAULT_CLASS_FILTER, classPass, isPpeViolation, parseClassFilter } from "./classFilter";

export type FeedEvent = Pick<NvrEvent, "status" | "camera_class"> & Partial<Pick<NvrEvent, "policy">>;

/** A vehicle event the verifier hasn't decided yet (still tracked, or waiting for YOLO). */
export const checkingVehicle = (e: FeedEvent): boolean =>
  e.camera_class === "vehicle" && (e.status === "open" || e.status === "pending");

/**
 * The feed's cards (in the order given) through the column's People / Vehicles / PPE pills (`filter`, classFilter.ts),
 * how many vehicles are left out while they are checked (only those the pills would show: none with Vehicles off), and
 * how many events the pills hid (`hidden`: "Nothing matches these filters" when no card is left).
 */
export function feedCards<T extends FeedEvent>(events: readonly T[], filter: ClassFilter = DEFAULT_CLASS_FILTER): { cards: T[]; checking: number; hidden: number } {
  const cards: T[] = [];
  let checking = 0, hidden = 0;
  for (const e of events) {
    const pass = classPass(e, filter);
    if (checkingVehicle(e)) { if (pass) checking++; }
    else if (pass) cards.push(e);
    else hidden++;
  }
  return { cards, checking, hidden };
}

/** The column's muted line for them: "Checking 3 vehicles…", or null when there are none. */
export function checkingText(n: number): string | null {
  return n > 0 ? `Checking ${n} ${n === 1 ? "vehicle" : "vehicles"}…` : null;
}

/** The column's pills are remembered per viewer (this browser), apart from the Timeline's ("timelineFilter"). */
export const FEED_FILTER_KEY = "liveFeedFilter";

/** The viewer's pills, or the defaults (People and Vehicles on, PPE off) when none are stored or storage is blocked. */
export function loadFeedFilter(key: string = FEED_FILTER_KEY): ClassFilter {
  try {
    return parseClassFilter(localStorage.getItem(key));
  } catch {
    return { ...DEFAULT_CLASS_FILTER };
  }
}

export function saveFeedFilter(f: ClassFilter, key: string = FEED_FILTER_KEY): void {
  try {
    localStorage.setItem(key, JSON.stringify(f));
  } catch {
    /* private mode: the choice lasts for this page only */
  }
}
