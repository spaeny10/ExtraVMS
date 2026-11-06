import { describe, expect, it } from "vitest";
import { type AxisDevice, scenarioText, setupMessage, setupState } from "./axis";

const base: AxisDevice = {
  supported: true,
  identity: { brand: "AXIS", model: "AXIS M4327-P", firmware: "11.4.63", apis: { "analytics-metadata-config": "1.0" } },
  firmware: { version: "11.4.63", old: true, baseline: "AXIS OS 11.11" },
  metadata: { supported: true, producer: "AnalyticsSceneDescription", producer_name: "Fusion Tracker", enabled: false },
  analytics: { supported: true, scenarios: [] },
  streams: { main_path: "/onvif-media/media.amp?profile=profile_0_h264", sub_path: "/onvif-media/media.amp?profile=profile_2_h264", vapix: false, analytics: false },
  setup_needed: true,
};

describe("axis", () => {
  it("names scenarios and what they open", () => {
    expect(scenarioText({ id: 1, name: "Door", type: "motion", labels: ["person"], counting: false })).toBe("Object in area · people");
    expect(scenarioText({ id: 2, name: "Gate", type: "fence", labels: ["person", "vehicle"], counting: false })).toBe("Line crossing · people, vehicles");
    expect(scenarioText({ id: 3, name: "Count", type: "crosslineCounting", labels: ["person"], counting: true })).toBe("Crossline counting · counts only, opens no events");
  });

  it("says what is missing before setup and nothing after", () => {
    expect(setupState(base).map((r) => r.ok)).toEqual([false, false, false]);
    const done: AxisDevice = { ...base, metadata: { ...base.metadata, enabled: true } as AxisDevice["metadata"],
      streams: { main_path: "/axis-media/media.amp?camera=1&videocodec=h264&analytics=polygon", sub_path: "/axis-media/media.amp?camera=1&videocodec=h264&resolution=1024x1024", vapix: true, analytics: true } };
    expect(setupState(done).every((r) => r.ok)).toBe(true);
  });

  it("without scene metadata only the paths are offered", () => {
    const none: AxisDevice = { ...base, metadata: { supported: false, reason: "no producer" } };
    expect(setupState(none)[0].text).toContain("No scene metadata (no producer)");
    expect(setupMessage(none)).not.toContain("turns on");
    expect(setupMessage(base)).toContain("turns on the camera's scene metadata (Fusion Tracker)");
  });
});
