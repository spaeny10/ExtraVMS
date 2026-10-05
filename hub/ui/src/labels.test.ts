import { describe, expect, it } from "vitest";
import { KIND_LABEL, whereLabel } from "./labels";

describe("whereLabel", () => {
  const p = { site: "HQ", server: "Hailo T1", camera: "Front door" };
  it("Site · Server · Camera across the customer", () => {
    expect(whereLabel(p)).toBe("HQ · Hailo T1 · Front door");
    expect(whereLabel(p, { serverCount: 2 })).toBe("HQ · Hailo T1 · Front door");
  });
  it("drops the server when the Site has one", () => {
    expect(whereLabel(p, { serverCount: 1 })).toBe("HQ · Front door");
  });
  it("drops the Site inside a Site page", () => {
    expect(whereLabel(p, { showSite: false })).toBe("Hailo T1 · Front door");
    expect(whereLabel(p, { showSite: false, serverCount: 1 })).toBe("Front door");
  });
  it("skips missing parts and a server named like its Site", () => {
    expect(whereLabel({ site: null, server: "Box", camera: "cam1" })).toBe("Box · cam1");
    expect(whereLabel({ site: "Box", server: "Box", camera: "cam1" })).toBe("Box · cam1");
  });
});

describe("KIND_LABEL", () => {
  it("names the detector health alerts (hub alerts.py HEALTH_KINDS)", () => {
    expect(KIND_LABEL.detector_fallback).toBe("Detection on CPU");
    expect(KIND_LABEL.detector_stalled).toBe("Verification stalled");
  });
});
