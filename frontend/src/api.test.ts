import { expect, test } from "vitest";
import { api, makeApi, withQuery } from "./api";

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
