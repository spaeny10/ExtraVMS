import { afterEach, describe, expect, it, vi } from "vitest";
import { CHUNK, FAR_CHUNK, FIRST_FAR_CHUNK, chunkLen, dropPrefetch, firstChunkLen, nowS, prefetchChunk } from "./playback";

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
