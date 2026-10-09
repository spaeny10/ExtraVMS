import { expect, test } from "vitest";
import { cameraOfPath, stillWidth, streamsText, streamsTitle, type CameraStreams, type StreamProfile } from "./cameraStreams";

const main: StreamProfile = { token: "Profile_1", encoding: "H.264", width: 3840, height: 2160, fps: 25, path: "/main" };
const sub: StreamProfile = { token: "Profile_2", encoding: "H.265", width: 1280, height: 720, fps: 15, path: "/sub" };
const base = { checked_at: 1000, error: null, sub_not_found: false };

test("streamsText: two streams, main only, detected, unknown", () => {
  const two: CameraStreams = { ...base, profiles: [main, sub], main, sub, sd: { path: "/sub", relay: false, detected: false } };
  expect(streamsText(two)).toBe("Main 3840×2160 H.264 · Sub 1280×720 H.265");
  const one: CameraStreams = { ...base, profiles: [main], main, sub: null, sd: { path: null, relay: true, detected: false } };
  expect(streamsText(one)).toBe("Main 3840×2160 H.264 only");
  const other = { ...sub, path: "/stream2" };
  const detected: CameraStreams = { ...base, profiles: [main, other], main, sub: other, sd: { path: "/stream2", relay: false, detected: true } };
  expect(streamsText(detected)).toBe("Main 3840×2160 H.264 · Sub 1280×720 H.265 (/stream2)");
  // never checked: nothing to say; a 404 without a check: the relay is said
  expect(streamsText({ ...base, checked_at: null, profiles: [], main: null, sub: null, sd: { path: "/sub", relay: false, detected: false } })).toBe("");
  expect(streamsText({ ...base, checked_at: null, profiles: [], main: null, sub: null, sd: { path: null, relay: true, detected: false } })).toBe("Main only");
  expect(streamsText(undefined)).toBe("");
});

test("streamsTitle explains the SD source and the last check", () => {
  const one: CameraStreams = { ...base, profiles: [main], main, sub: null, sd: { path: null, relay: true, detected: false } };
  const t = streamsTitle(one, 1000 + 600);
  expect(t).toContain("SD live view plays the main stream");
  expect(t).toContain("/main 3840×2160 H.264");
  expect(t).toContain("Checked 10 min ago");
  expect(streamsTitle({ ...one, checked_at: null, error: "connection failed" })).toContain("Last check failed: connection failed");
});

test("stillWidth steps to what /api/frame serves", () => {
  expect(stillWidth(300)).toBe(320);
  expect(stillWidth(390, 3)).toBe(960);      // a phone: 390 CSS px, DPR capped at 2
  expect(stillWidth(640, 1)).toBe(640);
  expect(stillWidth(900, 2)).toBe(1280);
  expect(stillWidth(0)).toBe(320);
});

test("cameraOfPath", () => {
  expect(cameraOfPath("cam4_sub")).toBe("cam4");
  expect(cameraOfPath("cam4_hd")).toBe("cam4");
  expect(cameraOfPath("cam_sub_yard")).toBe("cam_sub_yard");
});
