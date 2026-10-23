import { describe, expect, it } from "vitest";
import { healthRows } from "./health";
import type { SourceSite } from "./source";

const srv = (id: string, over: Partial<SourceSite> = {}): SourceSite =>
  ({ id, name: id, online: true, cameras: 4, camerasUp: 4, openAlerts: 0, ...over });

describe("healthRows", () => {
  it("rolls a Site's servers into one row", () => {
    const rows = healthRows([
      srv("a", { locationId: "L1", locationName: "Yard" }),
      srv("b", { locationId: "L1", locationName: "Yard", cameras: 2, camerasUp: 2 }),
    ]);
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ name: "Yard", online: 2, cameras: 6, camerasUp: 6, problem: false, problemServers: [] });
  });

  it("keeps a server without a Site as its own row (the NVR's Home)", () => {
    const rows = healthRows([srv("nvr1", { name: "Office" })]);
    expect(rows.map((r) => r.name)).toEqual(["Office"]);
  });

  it("puts Sites with a problem first, then by name", () => {
    const rows = healthRows([
      srv("a", { locationId: "L1", locationName: "Alpha" }),
      srv("b", { locationId: "L2", locationName: "Bravo", openAlerts: 1 }),
      srv("c", { locationId: "L3", locationName: "Charlie" }),
      srv("d", { locationId: "L4", locationName: "Delta", camerasUp: 3 }),
    ]);
    expect(rows.map((r) => r.name)).toEqual(["Bravo", "Delta", "Alpha", "Charlie"]);
  });

  it("names the servers with a problem on a several-server Site, and counts an offline server's cameras as down", () => {
    const rows = healthRows([
      srv("a", { locationId: "L1", locationName: "Yard" }),
      srv("b", { locationId: "L1", locationName: "Yard", online: false }),
    ]);
    expect(rows[0].problemServers.map((s) => s.id)).toEqual(["b"]);
    expect(rows[0]).toMatchObject({ online: 1, cameras: 8, camerasUp: 4, problem: true });
  });

  it("lists no servers under a one-server Site", () => {
    expect(healthRows([srv("a", { locationId: "L1", locationName: "Yard", online: false })])[0].problemServers).toEqual([]);
  });
});
