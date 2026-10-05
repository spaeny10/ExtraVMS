import { describe, expect, it } from "vitest";
import { accessLabel, lastEventBySite, ofTotal, toggleAccess } from "./access";

describe("toggleAccess", () => {
  const a = { all_sites: false, location_ids: ["l_1"] };
  it("adds and removes a Site without duplicates", () => {
    expect(toggleAccess(a, { location: "l_2", on: true }).location_ids).toEqual(["l_1", "l_2"]);
    expect(toggleAccess(a, { location: "l_1", on: true }).location_ids).toEqual(["l_1"]);
    expect(toggleAccess(a, { location: "l_1", on: false }).location_ids).toEqual([]);
  });
  it("All sites keeps the list for later", () => {
    const all = toggleAccess(a, { all: true });
    expect(all).toEqual({ all_sites: true, location_ids: ["l_1"] });
    expect(toggleAccess(all, { all: false })).toEqual(a);
  });
});

describe("accessLabel", () => {
  const sites = [{ id: "l_1", name: "HQ" }, { id: "l_2", name: "Yard" }];
  it("names", () => {
    expect(accessLabel({ all_sites: true, location_ids: ["l_1"] }, sites)).toBe("All sites");
    expect(accessLabel({ all_sites: false, location_ids: [] }, sites)).toBe("No sites");
    expect(accessLabel({ all_sites: false, location_ids: ["l_2", "l_9"] }, sites)).toBe("Yard, l_9");
  });
});

describe("lastEventBySite", () => {
  it("maps server events to their Site and keeps the newest", () => {
    const servers = [{ id: "s_a", location_id: "l_1" }, { id: "s_b", location_id: "l_1" }, { id: "s_c", location_id: null }];
    const events = [{ site_id: "s_a", start_ts: 10 }, { site_id: "s_b", start_ts: 30 }, { site_id: "s_a", start_ts: 20 }, { site_id: "s_c", start_ts: 99 }, { site_id: "s_x", start_ts: 5 }];
    expect(lastEventBySite(events, servers)).toEqual({ l_1: 30 });
  });
});

describe("ofTotal", () => {
  it("formats", () => {
    expect(ofTotal(2, 3, "online")).toBe("2/3 online");
    expect(ofTotal(0, 0, "up")).toBe("—");
  });
});
