import { expect, test } from "vitest";
import type { Camera } from "./api";
import type { CameraStreams, StreamProfile } from "./cameraStreams";
import { FIELD_TAB, cameraWebUrl, changedFields, dirtyTabs, headerSummary, openingTab, visibleTabs, type CameraDraft } from "./cameraEdit";

const cam: Camera = {
  id: "cam1", name: "Side Yard", host: "10.0.0.5", onvif_port: 80, rtsp_port: 554, username: "admin", main_path: "/main",
  sub_path: "/sub", enabled: 1, zones: [], retention_days: null, scene_notes: "",
};
const main: StreamProfile = { token: "p1", encoding: "H.264", width: 2592, height: 1520, fps: 20, path: "/main" };
const sub: StreamProfile = { token: "p2", encoding: "H.264", width: 640, height: 480, fps: 15, path: "/sub" };
const streams: CameraStreams = { checked_at: 1, error: null, profiles: [main, sub], main, sub, sd: { path: "/sub", relay: false, detected: false } };
const text = (c: Camera | undefined) => headerSummary(c).map((p) => p.text).join(" · ");

test("headerSummary: everything known", () => {
  const c: Camera = {
    ...cam, streams, record_stream: "main",
    status: {
      stream_ready: true, recording: true, tracks: [],
      detections: { setting: "auto", source: "metadata", reason: "objects in metadata", motion_events: false },
      ptz: { available: true } as NonNullable<NonNullable<Camera["status"]>["ptz"]>,
    },
  };
  expect(text(c)).toBe("Side Yard · cam1 · streaming · Main 2592×1520 H.264 / Sub 640×480 H.264 · recording main · events from camera metadata · PTZ");
  expect(headerSummary(c)[2]).toEqual({ text: "streaming", tone: "ok" });
});

test("headerSummary: parts that don't apply are left out", () => {
  // no status, never checked: name, id, what it records
  expect(text(cam)).toBe("Side Yard · cam1 · recording main");
  // disabled: no stream state or recording
  expect(text({ ...cam, enabled: 0 })).toBe("Side Yard · cam1 · disabled");
  // down, main stream only, recording the sub stream, ONVIF events with motion
  const c: Camera = {
    ...cam, record_stream: "sub", event_source: "onvif_events", motion_events: 1,
    streams: { ...streams, sub: null, sd: { path: null, relay: true, detected: false } },
    status: { stream_ready: false, recording: false, tracks: [] },
  };
  expect(text(c)).toBe("Side Yard · cam1 · not streaming · Main 2592×1520 H.264 only · not recording · events from camera ONVIF events and motion alarms");
  expect(headerSummary(undefined)).toEqual([{ text: "New camera" }]);
});

test("dirtyTabs: each changed field marks its own tab", () => {
  const d = (patch: Partial<CameraDraft>) => [...dirtyTabs(cam, { ...cam, ...patch })];
  expect(d({})).toEqual([]);
  expect(d({ name: "Back Yard" })).toEqual(["general"]);
  expect(d({ password: "secret", public_host: "1.2.3.4" })).toEqual(["connection"]);
  expect(d({ record_stream: "sub" })).toEqual(["recording"]);
  expect(d({ retention_policy: { continuous_days: 7 } })).toEqual(["recording"]);
  expect(d({ synopsis_labels: ["person", "vehicle"] })).toEqual(["detection"]);
  expect(d({ policies: [{ kind: "towing", asset: "", allowed: [], priority: "high" }] })).toEqual(["rules"]);
  expect(new Set(d({ name: "x", sub_path: "/s2", event_source: "metadata" }))).toEqual(new Set(["general", "connection", "detection"]));
});

test("dirtyTabs: back to what it was, or a default spelled differently, is no change", () => {
  // ticked off and on again (1 -> true), password typed and cleared, defaults set explicitly, labels reordered
  expect(changedFields(cam, { ...cam, enabled: true, password: "", record_stream: "main", event_source: "auto", synopsis_labels: ["person"], policies: [] })).toEqual([]);
  expect(changedFields({ ...cam, synopsis_labels: ["vehicle", "person"] }, { ...cam, synopsis_labels: ["person", "vehicle"] })).toEqual([]);
  // status and streams are not form fields
  expect(changedFields(cam, { ...cam, streams, status: { stream_ready: true, recording: true, tracks: [] } })).toEqual([]);
  // a dropped override is a change (retention_days cleared)
  expect(changedFields({ ...cam, retention_days: 30 }, { ...cam, retention_days: null })).toEqual(["retention_days"]);
});

test("every tab named in FIELD_TAB is a visible tab", () => {
  const tabs = visibleTabs(true);
  for (const t of Object.values(FIELD_TAB)) expect(tabs).toContain(t);
});

test("visibleTabs and openingTab", () => {
  expect(visibleTabs(false)).not.toContain("ptz");
  expect(visibleTabs(true)).toEqual(["general", "connection", "recording", "detection", "journeys", "ptz", "rules"]);
  expect(openingTab(null, false, visibleTabs(false))).toBe("general");
  expect(openingTab("recording", false, visibleTabs(false))).toBe("recording");
  // a new camera always opens on General; a remembered PTZ tab on a camera without PTZ falls back to General
  expect(openingTab("recording", true, visibleTabs(false))).toBe("general");
  expect(openingTab("ptz", false, visibleTabs(false))).toBe("general");
  expect(openingTab("ptz", false, visibleTabs(true))).toBe("ptz");
});

test("cameraWebUrl: the camera's own page on the LAN", () => {
  expect(cameraWebUrl(cam)).toBe("http://10.0.0.5/");
  expect(cameraWebUrl({ ...cam, web_port: 8080 })).toBe("http://10.0.0.5:8080/");
  expect(cameraWebUrl({ ...cam, web_scheme: "https" })).toBe("https://10.0.0.5/");
  expect(cameraWebUrl({ ...cam, web_scheme: "https", web_port: 443 })).toBe("https://10.0.0.5/");
  expect(cameraWebUrl({ ...cam, web_scheme: "https", web_port: 80 })).toBe("https://10.0.0.5:80/");
  expect(cameraWebUrl({ ...cam, host: "fe80::1" })).toBe("http://[fe80::1]/");
  expect(cameraWebUrl({ ...cam, host: "" })).toBeNull();
  expect(cameraWebUrl({ ...cam, host: "evil.com/x@" })).toBeNull();
});

test("web page fields are edited on the Connection tab", () => {
  expect([FIELD_TAB.web_scheme, FIELD_TAB.web_port, FIELD_TAB.public_web_port]).toEqual(["connection", "connection", "connection"]);
  // an unset scheme and http are the same (no unsaved-changes dot)
  expect(changedFields({ ...cam, web_scheme: null }, { ...cam, web_scheme: "http" })).toEqual([]);
  expect(changedFields(cam, { ...cam, web_port: 8080 })).toEqual(["web_port"]);
});
