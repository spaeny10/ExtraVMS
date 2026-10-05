import { describe, expect, it } from "vitest";
import { yoloFallbackText, yoloState } from "./yoloStatus";

const fb = { wanted: "hailo", using: "cpu", since: 1000, error: "RuntimeError: HAILO_OUT_OF_PHYSICAL_DEVICES(74)" };

describe("YOLO status words", () => {
  it("says ready / loading without a fallback", () => {
    expect(yoloState({ yolo_ready: true, yolo_fallback: null })).toBe("Ready");
    expect(yoloState({ yolo_ready: false })).toBe("Loading");
  });
  it("says the CPU fallback and why", () => {
    expect(yoloState({ yolo_ready: true, yolo_fallback: fb })).toBe("Running on the CPU (Hailo unavailable)");
    const t = yoloFallbackText(fb, () => "03:12");
    expect(t).toContain("since 03:12");
    expect(t).toContain("HAILO_OUT_OF_PHYSICAL_DEVICES");
    expect(t).toContain("used again");
  });
  it("says detection is down when there is no CPU model", () => {
    const down = { ...fb, using: null };
    expect(yoloState({ yolo_ready: false, yolo_fallback: down })).toMatch(/^Down/);
    expect(yoloFallbackText(down, () => "x")).toContain("not being verified");
  });
});
