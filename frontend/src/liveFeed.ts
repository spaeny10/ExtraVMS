/**
 * Live → "Latest activity" (LiveView here, Site Live on the hub through @site/liveFeed): which events show as cards.
 * A vehicle the camera reported shows only once YOLO has verified it. Parked cars and cameras whose own vehicle
 * detection fires on nothing opened an event a minute (2026-10-19), each a "Waiting for verification… Tracking" card
 * that vanished a minute later when it was rejected, so the column was full of cards that were never anything.
 * People (and every other label) still show the moment they open. The tiles' badge still lights "vehicle" while one is
 * open (the callers' activeOf reads the whole list): only the cards wait, and a muted line counts them.
 */
import type { NvrEvent } from "./api";

export type FeedEvent = Pick<NvrEvent, "status" | "camera_class">;

/** A vehicle event the verifier hasn't decided yet (still tracked, or waiting for YOLO). */
export const checkingVehicle = (e: FeedEvent): boolean =>
  e.camera_class === "vehicle" && (e.status === "open" || e.status === "pending");

/** The feed's cards (in the order given) and how many vehicles are left out while they are checked. */
export function feedCards<T extends FeedEvent>(events: readonly T[]): { cards: T[]; checking: number } {
  const cards: T[] = [];
  let checking = 0;
  for (const e of events) {
    if (checkingVehicle(e)) checking++;
    else cards.push(e);
  }
  return { cards, checking };
}

/** The column's muted line for them: "Checking 3 vehicles…", or null when there are none. */
export function checkingText(n: number): string | null {
  return n > 0 ? `Checking ${n} ${n === 1 ? "vehicle" : "vehicles"}…` : null;
}
