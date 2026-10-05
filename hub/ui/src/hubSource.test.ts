import { describe, expect, it } from "vitest";
import { eventLink, liveLink } from "./hubSource";

const siteOf = (server: string) => ({ srvA: "loc1" } as Record<string, string>)[server];

describe("dashboard links", () => {
  it("opens an event on its Site's combined Timeline when the Site is known", () => {
    expect(eventLink(siteOf, { site_id: "srvA", camera_id: "cam1", id: 42 })).toBe("/sites/loc1/timeline?server=srvA&cam=cam1&event=42");
  });
  it("falls back to the server's console without a Site or a camera", () => {
    expect(eventLink(siteOf, { site_id: "srvB", camera_id: "cam1", id: 42 })).toBe("/s/srvB/#timeline?cam=cam1&event=42");
    expect(eventLink(siteOf, { site_id: "srvA", id: 42 })).toBe("/s/srvA/#timeline?cam=&event=42");
  });
  it("opens live on the Site, else on the server", () => {
    expect(liveLink(siteOf, "srvA")).toBe("/sites/loc1/live");
    expect(liveLink(siteOf, "srvB")).toBe("/s/srvB/#live");
  });
});
