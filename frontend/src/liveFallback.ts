/**
 * What a live tile does when its picture can't load: the recording fallback (LivePlayer's RecordingFollow) and the
 * paused tiles' still frames. Pure decisions, kept apart so they can be tested without a browser.
 */

/** A camera with no recording waits this long before trying WebRTC again (it used to bounce every few seconds). */
export const NO_RECORDING_RETRY_MS = 30_000;
/** Shown on a tile whose camera has nothing on disk for "now" (the Timeline's wording). */
export const NO_RECORDING_TEXT = "No recording at this time";

export type FollowStep = "no-recording" | "retry" | "give-up";

/**
 * After the recording chunk failed to load, from that URL's HTTP status (null = no answer) and how many failures in a
 * row so far (this one included): 404 = nothing recorded for that range, so stop and wait; anything else is retried a
 * few times, then the tile goes back to WebRTC.
 */
export function afterFollowError(status: number | null, errors: number): FollowStep {
  if (status === 404) return "no-recording";
  return errors >= 3 ? "give-up" : "retry";
}

/** The HTTP status of `url` without downloading it (the body is cancelled once the headers are in); null = no answer. */
export async function httpStatus(url: string): Promise<number | null> {
  try {
    const r = await fetch(url, { cache: "no-store" });
    r.body?.cancel().catch(() => {});
    return r.status;
  } catch {
    return null;
  }
}

/** Paused tiles refresh their still this often... */
export const STILL_REFRESH_MS = 10_000;
/** ...and only this often once a still failed to load (no recording, server not answering), until one loads again. */
export const STILL_BACKOFF_MS = 60_000;
/** Shown where a still couldn't load. */
export const NO_PICTURE_TEXT = "No picture";

/** How long until the next still: the slow timer while the last one failed. */
export const stillDelay = (failed: boolean): number => (failed ? STILL_BACKOFF_MS : STILL_REFRESH_MS);

/** Stills on one shared 10 s tick (the phone's strip): may one that failed on tick `failedAt` (undefined = it didn't)
 *  load again on `tick`? Once a minute. */
export const stillDue = (tick: number, failedAt: number | undefined): boolean =>
  failedAt === undefined || tick - failedAt >= STILL_BACKOFF_MS / STILL_REFRESH_MS;

/** A still older than this shows the stale badge on the picture itself; younger ones only say their age in the bar. */
export const STALE_STILL_S = 30;

/** The still width to ask /api/frame for, from the tile's width on screen: 320 for a small tile, else 640 (the spec's
 *  two sizes; the server's floor is 320). */
export const stillWidth = (cssPx: number, dpr = 1): 320 | 640 => (cssPx * dpr < 480 ? 320 : 640);

/** "8 s", "3 min", "2 h": how old a still frame is (a frame "from the future" by clock skew reads 0 s). */
export function stillAgeText(ageS: number): string {
  const s = Math.max(0, ageS);
  if (s < 60) return `${Math.round(s)} s`;
  if (s < 3600) return `${Math.round(s / 60)} min`;
  return `${Math.round(s / 3600)} h`;
}
