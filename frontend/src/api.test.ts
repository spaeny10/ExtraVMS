import { afterEach, describe, expect, it, test, vi } from "vitest";
import { api, makeApi, probeHealth, whepSession, withQuery } from "./api";

test("makeApi builds every backend URL under its site prefix", () => {
  const s = makeApi("/s/s_abc");
  expect(s.base).toBe("/s/s_abc");
  expect(s.whepUrl("cam1_sub")).toBe("/s/s_abc/api/whep/cam1_sub");
  expect(s.media({ id: 42 }, "snapshot.jpg")).toBe("/s/s_abc/api/events/42/media/snapshot.jpg");
  expect(s.playbackUrl("cam1", 1000, 300)).toBe("/s/s_abc/api/playback/cam1?start=1000&duration=300");
  expect(s.frameUrl("cam2", 12.345, 320)).toBe("/s/s_abc/api/frame/cam2?t=12.35&w=320");
  expect(s.frameUrl("cam2", 1, 960, true)).toContain("exact=true");
});

test("the page's own site has no prefix outside the hub", () => {
  expect(api.base).toBe("");
  expect(api.whepUrl("cam1")).toBe("/api/whep/cam1");
  expect(makeApi("").media({ id: 1 }, "clip.mp4")).toBe("/api/events/1/media/clip.mp4");
});

test("playbackUrl asks for the SD transcode only when told to", () => {
  const s = makeApi("/s/s_abc");
  expect(s.playbackUrl("cam1", 1000, 60, "sd")).toBe("/s/s_abc/api/playback/cam1?start=1000&duration=60&q=sd");
  expect(s.playbackUrl("cam1", 1000, 60, "hd")).toBe("/s/s_abc/api/playback/cam1?start=1000&duration=60");
});

test("a direct client puts its token on every URL and its socket on the server's origin", () => {
  const d = makeApi("https://192.168.1.10:8443", { query: { direct: "t.k" } });
  expect(d.media({ id: 3 }, "snapshot.jpg")).toBe("https://192.168.1.10:8443/api/events/3/media/snapshot.jpg?direct=t.k");
  expect(d.frameUrl("c", 1, 320)).toBe("https://192.168.1.10:8443/api/frame/c?t=1.00&w=320&direct=t.k");
  expect(d.whepUrl("c_sub")).toBe("https://192.168.1.10:8443/api/whep/c_sub?direct=t.k");
  expect(d.wsUrl()).toBe("wss://192.168.1.10:8443/api/ws?direct=t.k");
  expect(withQuery("/a", {})).toBe("/a");
});

test("WHEP sessions: the id comes from the answer's Location, the hang-up URL from the same client", () => {
  const id = "0b6c1f8e-2d3a-4e5f-8a9b-1c2d3e4f5a6b";
  expect(whepSession(`/api/whep/cam1_sub/${id}`, "cam1_sub")).toBe(id);
  expect(whepSession(`/s/s_abc/api/whep/cam1_sub/${id}`, "cam1_sub")).toBe(id);   // through the hub
  for (const bad of [null, undefined, "", `/api/whep/cam2/${id}`, "/api/whep/cam1_sub/abc", `/api/whep/cam1_sub/${id}/x`]) {
    expect(whepSession(bad, "cam1_sub")).toBeNull();
  }
  expect(makeApi("/s/s_abc").whepSessionUrl("cam1_sub", id)).toBe(`/s/s_abc/api/whep/cam1_sub/${id}`);
  expect(makeApi("https://192.168.1.10:8443", { query: { direct: "t.k" } }).whepSessionUrl("c", id))
    .toBe(`https://192.168.1.10:8443/api/whep/c/${id}?direct=t.k`);
});

describe("probeHealth", () => {
  afterEach(() => vi.unstubAllGlobals());
  const answer = (status: number, type: string, body: string) =>
    vi.stubGlobal("fetch", vi.fn(async () => new Response(body, { status, headers: { "content-type": type } })));
  it("counts only JSON {ok: true}", async () => {
    answer(200, "application/json", '{"ok":true}');
    expect(await probeHealth()).toBe(true);
    expect(vi.mocked(fetch)).toHaveBeenCalledWith("/api/health", { cache: "no-store" });
  });
  it("an HTML page, an error or another answer is not healthy", async () => {
    answer(200, "text/html", "<html></html>");
    expect(await probeHealth()).toBe(false);
    answer(200, "application/json", '{"ok":false}');
    expect(await probeHealth()).toBe(false);
    answer(200, "application/json", "not json");
    expect(await probeHealth()).toBe(false);
    answer(503, "application/json", '{"ok":true}');
    expect(await probeHealth()).toBe(false);
  });
});
