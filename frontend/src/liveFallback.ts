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
