import { expect, test } from "vitest";
import { api, makeApi } from "./api";

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
