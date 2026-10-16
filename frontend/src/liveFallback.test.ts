import { afterEach, describe, expect, it, vi } from "vitest";
import {
  NO_RECORDING_RETRY_MS, STILL_BACKOFF_MS, STILL_REFRESH_MS, afterFollowError, httpStatus, stillDelay, stillDue,
} from "./liveFallback";

describe("recording fallback after a chunk fails", () => {
  it("a 404 means nothing is recorded for now: stop at once, whatever the count", () => {
    expect(afterFollowError(404, 1)).toBe("no-recording");
    expect(afterFollowError(404, 5)).toBe("no-recording");
  });
  it("other failures are retried, then hand back to WebRTC after the third", () => {
    expect(afterFollowError(null, 1)).toBe("retry");
    expect(afterFollowError(503, 2)).toBe("retry");
    expect(afterFollowError(200, 2)).toBe("retry");   // loaded but didn't play
    expect(afterFollowError(503, 3)).toBe("give-up");
    expect(afterFollowError(null, 4)).toBe("give-up");
  });
  it("waits a slow 30 s before trying live again", () => {
    expect(NO_RECORDING_RETRY_MS).toBe(30_000);
  });
});

describe("httpStatus", () => {
  afterEach(() => vi.unstubAllGlobals());
  it("reads the status and cancels the body", async () => {
    const cancel = vi.fn(() => Promise.resolve());
    const fetch = vi.fn(async () => ({ status: 404, body: { cancel } }));
    vi.stubGlobal("fetch", fetch);
    expect(await httpStatus("/s/x/api/playback/cam1?start=1")).toBe(404);
    expect(fetch).toHaveBeenCalledWith("/s/x/api/playback/cam1?start=1", { cache: "no-store" });
    expect(cancel).toHaveBeenCalled();
  });
  it("is null when nothing answers", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("network"); }));
    expect(await httpStatus("/x")).toBeNull();
  });
});

describe("still backoff", () => {
  it("refreshes every 10 s, every 60 s after a failure, and back to 10 s once one loads", () => {
    expect(stillDelay(false)).toBe(STILL_REFRESH_MS);
    expect(stillDelay(true)).toBe(STILL_BACKOFF_MS);
    expect(STILL_REFRESH_MS).toBe(10_000);
    expect(STILL_BACKOFF_MS).toBe(60_000);
  });
  it("on a shared 10 s tick, a failed still tries again only once a minute", () => {
    expect(stillDue(7, undefined)).toBe(true);
    expect(stillDue(3, 3)).toBe(false);
    expect(stillDue(8, 3)).toBe(false);
    expect(stillDue(9, 3)).toBe(true);
  });
});
