import { expect, test } from "vitest";
import {
  audioChanges, audioDraft, audioEncodings, audioPick, browserMute, clockSummary, encoderTitle, govFrames, offsetBad, offsetText,
  slowSeeking, splitServers, utcOffsetText, videoChanges, videoDraft, videoProblems, zoneCheckText, zoneList,
  type AudioEncoder, type VideoEncoder,
} from "./cameraDevice";

// a Milesight main stream (Media): H.264 2592x1520 at 25 fps, a keyframe every 4 s
const main: VideoEncoder = {
  token: "VE_1", name: "VE_1", encoding: "H264", encoding_raw: "H264", width: 2592, height: 1520, fps: 25, bitrate_kbps: 6144,
  gov_length: 100, gov_seconds: 4, quality: 4, cbr: null, profile: "High", profiles: ["Profile_1"], profile_name: "Profile_1",
  source: "VideoSource_1", media: "media", stream: "main",
  options: { quality: { min: 1, max: 6 }, encodings: [{
    encoding: "H264", encoding_raw: "H264", resolutions: [{ width: 2592, height: 1520 }, { width: 1920, height: 1080 }],
    fps: { min: 1, max: 25 }, fps_list: [], bitrate: { min: 64, max: 16384 }, gov: { min: 1, max: 250 },
  }] },
};
// the Uniview thermal (Media2): a list of frame rates, H.264 and H.265
const thermal: VideoEncoder = {
  ...main, token: "ve2", media: "media2", encoding: "H264", width: 1280, height: 960, stream: null, profile_name: "media_profile2_1",
  source: "VideoSource_2", fps: 25, gov_length: 250, gov_seconds: 10,
  options: { quality: null, encodings: ["H264", "H265"].map((e) => ({
    encoding: e, encoding_raw: e, resolutions: [{ width: 1280, height: 960 }], fps: { min: 1, max: 25 }, fps_list: [1, 5, 10, 15, 20, 25],
    bitrate: { min: 128, max: 8192 }, gov: { min: 1, max: 250 },
  })) },
};

test("offsets as text, red past 5 s", () => {
  expect(offsetText(2703)).toBe("45 min 3 s ahead");
  expect(offsetText(-3600)).toBe("1 h behind");
  expect(offsetText(-3725)).toBe("1 h 2 min behind");
  expect(offsetText(0.4)).toBe("in step");
  expect(offsetText(-12)).toBe("12 s behind");
  expect(offsetText(null)).toBe("unknown");
  expect(offsetBad(5)).toBe(false);
  expect(offsetBad(-5.5)).toBe(true);
  expect(offsetBad(null)).toBe(false);
  expect(utcOffsetText(-18000)).toBe("UTC−5:00");
  expect(utcOffsetText(19800)).toBe("UTC+5:30");
  expect(zoneCheckText({ zone: "America/Chicago", expected_s: -18000, camera_s: -21600, ok: false }))
    .toBe("The camera's local time is UTC−6:00, America/Chicago is UTC−5:00 now: its time zone or daylight saving is wrong.");
  expect(zoneCheckText({ zone: "America/Chicago", expected_s: -18000, camera_s: -18000, ok: true })).toBeNull();
});

test("keyframe seconds become frames at the frame rate", () => {
  expect(govFrames(1, 25)).toBe(25);
  expect(govFrames(2, 15)).toBe(30);
  expect(govFrames(0.01, 25)).toBe(1);
  expect(slowSeeking(4)).toBe(true);
  expect(slowSeeking(2)).toBe(false);
});

test("videoChanges: only what differs; seconds go along with a new frame rate", () => {
  const d = videoDraft(main);
  expect(d).toEqual({ encoding: "H264", resolution: "2592x1520", fps: "25", bitrate_kbps: "6144", gov_seconds: "4" });
  expect(videoChanges(main, d)).toEqual({});
  expect(videoChanges(main, { ...d, gov_seconds: "1" })).toEqual({ gov_seconds: 1 });
  expect(videoChanges(main, { ...d, fps: "20" })).toEqual({ fps: 20, gov_seconds: 4 });
  expect(videoChanges(main, { ...d, resolution: "1920x1080", bitrate_kbps: "4096" })).toEqual({ resolution: "1920x1080", bitrate_kbps: 4096 });
});

test("videoProblems: the camera's options, as the server checks them", () => {
  const d = videoDraft(main);
  expect(videoProblems(main, { ...d, gov_seconds: "1" })).toEqual([]);
  expect(videoProblems(main, { ...d, fps: "30" })).toEqual(["Frame rate must be 1–25 fps."]);
  expect(videoProblems(main, { ...d, bitrate_kbps: "20000" })).toEqual(["Bitrate must be 64–16384 kbps."]);
  expect(videoProblems(main, { ...d, resolution: "640x480" })).toEqual(["640×480 is not offered for H.264."]);
  expect(videoProblems(main, { ...d, gov_seconds: "20" })).toEqual(["A keyframe every 20 s is 500 frames; the camera allows 1–250 (0.04–10 s at 25 fps)."]);
  expect(videoProblems(main, { ...d, gov_seconds: "0" })).toEqual(["The keyframe interval must be more than 0 s."]);
  expect(videoProblems(main, { ...d, fps: "abc" })).toEqual(["Enter numbers only."]);
  // Media2: frame rates from the camera's list
  expect(videoProblems(thermal, { ...videoDraft(thermal), fps: "18" })).toEqual(["18 fps is not offered (1, 5, 10, 15, 20, 25)."]);
  expect(videoProblems(thermal, { ...videoDraft(thermal), fps: "20", gov_seconds: "2" })).toEqual([]);
  expect(videoProblems(thermal, { ...videoDraft(thermal), encoding: "JPEG" })).toEqual(["The camera lists no options for MJPEG."]);
});

test("encoderTitle: what the stream is used for, and which sensor", () => {
  expect(encoderTitle(main, ["VideoSource_1"])).toBe("Main stream (recorded)");
  expect(encoderTitle({ ...main, stream: "sub" }, ["VideoSource_1"])).toBe("Sub stream (SD live view)");
  expect(encoderTitle({ ...main, stream: "sub" }, ["VideoSource_1"], "sub")).toBe("Sub stream (recorded)");
  expect(encoderTitle(thermal, ["VideoSource_1", "VideoSource_2"])).toBe("media_profile2_1 · sensor 2");
});

const g711: AudioEncoder = {
  token: "AE_1", name: "AE_1", encoding: "G711", encoding_raw: "G711", bitrate: 64, sample_rate: 8, profiles: ["Profile_1"], media: "media",
  options: [
    { encoding: "G711", encoding_raw: "G711", bitrates: [64], sample_rates: [8] },
    { encoding: "G711", encoding_raw: "PCMA", bitrates: [64], sample_rates: [8] },
    { encoding: "AAC", encoding_raw: "MP4A-LATM", bitrates: [32, 48], sample_rates: [16] },
  ],
};

test("audio: AAC moves the bitrate and sample rate to what it offers", () => {
  expect(audioEncodings(g711)).toEqual(["G711", "AAC"]);
  const d = audioDraft(g711);
  expect(audioChanges(g711, d)).toEqual({});
  const aac = audioPick(g711, d, "AAC");
  expect(aac).toEqual({ encoding: "AAC", bitrate: "48", sample_rate: "16" });
  expect(audioChanges(g711, aac)).toEqual({ encoding: "AAC", bitrate: 48, sample_rate: 16 });
  expect(browserMute("G711")).toBe(true);
  expect(browserMute("AAC")).toBe(false);
});

test("servers, zones, summary", () => {
  expect(splitServers(" pool.ntp.org, 192.168.105.1  time.google.com;")).toEqual(["pool.ntp.org", "192.168.105.1", "time.google.com"]);
  const zones = zoneList(["America/Chicago", null, "CST6CDT"]);
  expect(zones[0]).toBe("America/Chicago");
  expect(zones).toContain("UTC");
  expect(zones).not.toContain("CST6CDT");
  expect(new Set(zones).size).toBe(zones.length);
  expect(clockSummary([{ camera_id: "a", name: "A", ok: true }, { camera_id: "b", name: "B", ok: true }, { camera_id: "c", name: "C", ok: false }]))
    .toBe("2 fixed · 1 failed");
  expect(clockSummary([])).toBe("no cameras");
});
