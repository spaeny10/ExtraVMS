import { describe, expect, it } from "vitest";
import { parseTimelineHash, timelineHash } from "./nav";

describe("timeline hash", () => {
  it("round-trips an event without a server (the site UI)", () => {
    const h = timelineHash("cam1", 123, true, 1790270080);
    expect(h).toBe("#timeline?cam=cam1&event=123&journey=1");
    expect(parseTimelineHash(h)).toMatchObject({ cam: "cam1", event: 123, journey: true, t: null, region: null, server: null });
  });
  it("carries the server for the hub's combined Timeline", () => {
    const h = timelineHash("cam2", 0, false, 1790270080.4, null, "srv a");
    expect(h).toBe("#timeline?cam=cam2&t=1790270080&server=srv%20a");
    expect(parseTimelineHash(h)).toMatchObject({ cam: "cam2", event: null, t: 1790270080, server: "srv a" });
  });
  it("ignores other hashes", () => {
    expect(parseTimelineHash("#live")).toBeNull();
  });
});
