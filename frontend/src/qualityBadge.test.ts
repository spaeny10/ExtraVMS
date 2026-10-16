import { describe, expect, it } from "vitest";
import { MODE_LABEL, QUALITY_MODES, qualityBadge } from "./qualityBadge";

describe("qualityBadge", () => {
  it("Auto shows what it picked while live, only the mode while paused", () => {
    expect(qualityBadge("auto", false).text).toBe("Auto · SD");
    expect(qualityBadge("auto", true).text).toBe("Auto · HD");
    expect(qualityBadge("auto", true).label).toBe("Stream quality: Auto, now HD");
    expect(qualityBadge("auto", false, false).text).toBe("Auto");
    expect(qualityBadge("auto", false, false).label).toBe("Stream quality: Auto");
  });
  it("a viewer's pick shows as SD or HD", () => {
    expect(qualityBadge("sd", false).text).toBe("SD");
    expect(qualityBadge("hd", true).text).toBe("HD");
    expect(qualityBadge("hd", true).stalled).toBe(false);
    // paused tile: hd is not known (false), the pick still reads HD
    expect(qualityBadge("hd", false, false).text).toBe("HD");
  });
  it("HD picked but SD plays: says so and offers a retry", () => {
    const b = qualityBadge("hd", false);
    expect(b.text).toBe("HD · SD");
    expect(b.stalled).toBe(true);
    expect(b.title).toMatch(/retry/);
  });
  it("HD this browser can't play: the reason is the tooltip, not a stall", () => {
    const b = qualityBadge("hd", false, true, "H.265: this browser can't play it");
    expect(b.stalled).toBe(false);
    expect(b.text).toBe("HD");
    expect(b.title).toMatch(/^H\.265/);
    expect(qualityBadge("auto", false, true, "H.265 here").title).toMatch(/H\.265 here/);
  });
  it("the menu lists Auto, SD, HD", () => {
    expect(QUALITY_MODES.map((m) => MODE_LABEL[m])).toEqual(["Auto", "SD", "HD"]);
  });
});
