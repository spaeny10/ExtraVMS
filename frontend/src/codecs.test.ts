import { describe, expect, it } from "vitest";
import { canPlayLive, codecCache, pathCodec, type LiveCodecs } from "./codecs";

const caps = (...mime: string[]) => ({ codecs: mime.map((mimeType) => ({ mimeType })) });

describe("canPlayLive", () => {
  it("H.264 and unknown codecs are always tried", () => {
    expect(canPlayLive("H264", caps("video/VP8"))).toBe(true);
    expect(canPlayLive(null, caps())).toBe(true);
  });
  it("H.265 only where the browser lists an H.265 decoder", () => {
    expect(canPlayLive("H265", caps("video/VP8", "video/H264"))).toBe(false);
    expect(canPlayLive("H265", caps("video/H264", "video/H265"))).toBe(true);
    expect(canPlayLive("H265", caps("video/h265"))).toBe(true);
    expect(canPlayLive("H265", caps("video/HEVC"))).toBe(true);
    expect(canPlayLive("H265", caps("video/H265-rtx"))).toBe(false);
  });
  it("a browser that can't list its decoders tries H.265 too (the player's own check is the safety net)", () => {
    expect(canPlayLive("H265", null)).toBe(true);
  });
  it("reads the path's codec from the server's list", () => {
    const list: LiveCodecs = { cam1: { video: "H265", ready: true }, cam1_sub: { video: null, ready: false } };
    expect(pathCodec(list, "cam1")).toBe("H265");
    expect(pathCodec(list, "cam1_sub")).toBe(null);
    expect(pathCodec(list, "cam2")).toBe(null);
    expect(pathCodec(null, "cam1")).toBe(null);
  });
});

describe("codecCache", () => {
  it("one request per server while fresh, then again; failures count as no list", async () => {
    let t = 0;
    const c = codecCache(30_000, () => t);
    let loads = 0;
    const load = async () => { loads++; return { cam1: { video: "H264" as const, ready: true } }; };
    expect(c.last("/s/a")).toBeUndefined();
    const [a, b] = await Promise.all([c.get("/s/a", load), c.get("/s/a", load)]);
    expect(a).toEqual(b);
    expect(loads).toBe(1);
    expect(c.last("/s/a")).toEqual(a);
    await c.get("/s/b", load);
    expect(loads).toBe(2);   // per server
    t = 31_000;
    await c.get("/s/a", load);
    expect(loads).toBe(3);
    expect(await c.get("/s/old", () => Promise.reject(new Error("404")))).toBeNull();
    expect(c.last("/s/old")).toBeNull();
  });
});
