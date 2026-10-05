import { afterEach, describe, expect, it, vi } from "vitest";
import {
  CHUNK, DRIFT_RELOAD_MIN_S, FAR_CHUNK, FIRST_FAR_CHUNK, HOLD_MAX_MS, REMOTE_CHUNK, REMOTE_FIRST_CHUNK, RETRY_STEADY_MS, STALL_S,
  camKey, chunkLen, driftReloadAllowed, dropPrefetch, firstChunkLen, nowS, prefetchChunk, retryDelayMs, shouldReload, splitKey, LIVE_LAG, reachesLiveEdge,
} from "./playback";

describe("chunk lengths", () => {
  it("near live: long chunks; far back: a short first chunk then full ones", () => {
    const recent = nowS() - 60;
    const far = nowS() - 3 * 86400;
    expect(firstChunkLen(recent)).toBe(CHUNK);
    expect(chunkLen(recent)).toBe(CHUNK);
    expect(firstChunkLen(far)).toBe(FIRST_FAR_CHUNK);
    expect(chunkLen(far)).toBe(FAR_CHUNK);
    expect(FIRST_FAR_CHUNK).toBeLessThan(FAR_CHUNK);
  });
});

describe("chunk lengths through the hub (remote)", () => {
  it("short first chunk and 60 s continuations, near live and far back alike", () => {
    for (const t of [nowS() - 60, nowS() - 3 * 86400]) {
      expect(firstChunkLen(t, true)).toBe(REMOTE_FIRST_CHUNK);
      expect(chunkLen(t, true)).toBe(REMOTE_CHUNK);
    }
    expect(REMOTE_FIRST_CHUNK).toBe(20);
    expect(REMOTE_CHUNK).toBe(60);
  });
  it("local values are unchanged when remote is false or omitted", () => {
    const recent = nowS() - 60;
    expect(chunkLen(recent, false)).toBe(CHUNK);
    expect(firstChunkLen(recent, false)).toBe(CHUNK);
    expect(chunkLen(nowS() - 3 * 86400, false)).toBe(FAR_CHUNK);
  });
});

describe("stall decision (shouldReload)", () => {
  const base = { clockRel: 30, bufferedEnd: 20, lastProgressAt: 100, now: 102, remote: true, len: 60 };
  it("never reloads while the clock is within the slack of the buffered end", () => {
    expect(shouldReload({ ...base, clockRel: 23, remote: false })).toBe(false);
    expect(shouldReload({ ...base, clockRel: 23, now: 1000 })).toBe(false);
  });
  it("local: reloads as soon as the clock is well past the buffer (as before)", () => {
    expect(shouldReload({ ...base, remote: false })).toBe(true);
  });
  it("remote: waits while the download is progressing", () => {
    expect(shouldReload(base)).toBe(false);
    expect(shouldReload({ ...base, now: base.lastProgressAt + STALL_S - 0.1 })).toBe(false);
  });
  it("remote: reloads once nothing has progressed for STALL_S", () => {
    expect(shouldReload({ ...base, now: base.lastProgressAt + STALL_S })).toBe(true);
  });
  it("remote: reloads at once after a seek, or when the clock is more than two chunks past", () => {
    expect(shouldReload({ ...base, jumped: true })).toBe(true);
    expect(shouldReload({ ...base, clockRel: 20 + 2 * 60 + 1 })).toBe(true);
    expect(shouldReload({ ...base, clockRel: 20 + 2 * 60 - 1 })).toBe(false);
  });
  it("past the chunk end the slack doesn't apply, but a live download is still waited for", () => {
    expect(shouldReload({ ...base, clockRel: 61, bufferedEnd: 60, pastChunkEnd: true, remote: false })).toBe(true);
    expect(shouldReload({ ...base, clockRel: 61, bufferedEnd: 60, pastChunkEnd: true })).toBe(false);
    expect(shouldReload({ ...base, clockRel: 61, bufferedEnd: 60, pastChunkEnd: true, now: 100 + STALL_S })).toBe(true);
  });
});

describe("drift reload rate limit and clock hold", () => {
  it("allows one drift reload per DRIFT_RELOAD_MIN_S", () => {
    expect(driftReloadAllowed(null, 50, true)).toBe(true);
    expect(driftReloadAllowed(50, 50 + DRIFT_RELOAD_MIN_S.remote - 1, true)).toBe(false);
    expect(driftReloadAllowed(50, 50 + DRIFT_RELOAD_MIN_S.remote, true)).toBe(true);
    expect(driftReloadAllowed(50, 50 + DRIFT_RELOAD_MIN_S.local, false)).toBe(true);
    expect(DRIFT_RELOAD_MIN_S).toEqual({ local: 5, remote: 15 });
  });
  it("holds 3 s locally, 20 s remote", () => {
    expect(HOLD_MAX_MS).toEqual({ local: 3000, remote: 20000 });
  });
});

describe("recordings retry backoff", () => {
  it("2 s, 5 s, 10 s, then every 30 s", () => {
    expect([0, 1, 2, 3, 4, 50].map(retryDelayMs)).toEqual([2000, 5000, 10000, 30000, 30000, 30000]);
    expect(RETRY_STEADY_MS).toBe(30000);
  });
});

describe("prefetch", () => {
  const realFetch = globalThis.fetch;
  afterEach(() => { globalThis.fetch = realFetch; });

  it("holds the downloaded chunk as a blob URL, and abort/revoke on drop", async () => {
    const blob = new Blob([new Uint8Array(16)]);
    globalThis.fetch = vi.fn(async () => new Response(blob, { status: 200 })) as unknown as typeof fetch;
    const created: string[] = [];
    const revoked: string[] = [];
    (URL as unknown as { createObjectURL: (b: Blob) => string }).createObjectURL = (b: Blob) => { const u = `blob:${created.length}`; created.push(u); void b; return u; };
    (URL as unknown as { revokeObjectURL: (u: string) => void }).revokeObjectURL = (u: string) => { revoked.push(u); };
    const p = prefetchChunk("/api/playback/cam1?start=1&duration=120", 1, 120);
    expect(p.start).toBe(1);
    expect(p.len).toBe(120);
    await vi.waitFor(() => expect(p.done).toBe(true));
    expect(p.url).toBe("blob:0");
    dropPrefetch(p);
    expect(revoked).toEqual(["blob:0"]);
  });

  it("a failed download leaves no URL, so the tile falls back to the network", async () => {
    globalThis.fetch = vi.fn(async () => new Response("nope", { status: 503 })) as unknown as typeof fetch;
    const p = prefetchChunk("/api/playback/cam1?start=1&duration=120", 1, 120);
    await vi.waitFor(() => expect(p.done).toBe(true));
    expect(p.url).toBeNull();
  });
});

describe("camera keys", () => {
  it("this server's cameras keep their bare id", () => {
    expect(camKey("", "cam1")).toBe("cam1");
    expect(splitKey("cam1")).toEqual({ server: "", id: "cam1" });
  });
  it("another server's cameras are server/id and split back", () => {
    expect(camKey("srv_a", "cam1")).toBe("srv_a/cam1");
    expect(splitKey("srv_a/cam1")).toEqual({ server: "srv_a", id: "cam1" });
    expect(splitKey(camKey("s", "c_sub"))).toEqual({ server: "s", id: "c_sub" });
  });
});

describe("live edge (reachesLiveEdge)", () => {
  const now = 1_000_000;
  it("switches to live when a 1x clock would pass now - LIVE_LAG this frame", () => {
    expect(reachesLiveEdge(now - LIVE_LAG - 0.01, 0.016, 1, now)).toBe(true);
    expect(reachesLiveEdge(now - LIVE_LAG - 10, 0.016, 1, now)).toBe(false);
  });
  it("fast forward reaches it sooner; rewind and slow motion never do", () => {
    expect(reachesLiveEdge(now - LIVE_LAG - 0.05, 0.016, 4, now)).toBe(true);
    expect(reachesLiveEdge(now - LIVE_LAG, 0.016, -1, now)).toBe(false);
    expect(reachesLiveEdge(now - LIVE_LAG, 0.016, 0.5, now)).toBe(false);
  });
});
