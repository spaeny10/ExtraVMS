import { describe, expect, it } from "vitest";
import type { HubSitesOrg, Server, Site } from "./api";
import { groupByCustomer } from "./hubAdmin";

const srv = (id: string, online: boolean, extra: Partial<Server> = {}): Server => ({
  id, org_id: "o", name: id, location: "", online, last_seen_at: null, version: null, hostname: null, clock_skew_s: null,
  summary: {}, open_alerts: 0, ...extra,
});
const site = (id: string, name: string, servers: Server[], extra: Partial<Site> = {}): Site => ({
  id, org_id: "o", name, address: "", timezone: null, notes: null, created_at: 0, updated_at: 0, servers_total: servers.length,
  servers_online: servers.filter((s) => s.online).length, cameras_total: 0, cameras_online: 0, open_alerts: 0, retired_servers: 0,
  servers, ...extra,
});
const rows: HubSitesOrg[] = [
  { org: { id: "o_b", name: "beta" }, locations: [site("l_y", "Yard", [srv("s1", true)]), site("l_a", "annex", [srv("s2", false)], { open_alerts: 2 })], unassigned: [] },
  { org: { id: "o_a", name: "Alpha" }, locations: [site("l_hq", "HQ", [srv("s3", true)], { address: "1 Main St" })], unassigned: [srv("s4", false, { open_alerts: 1 })] },
  { org: { id: "o_c", name: "Gamma" }, locations: [], unassigned: [] },
];

describe("groupByCustomer", () => {
  it("orders customers and their Sites by name, ignoring case, and sums the header counts", () => {
    const g = groupByCustomer(rows);
    expect(g.map((x) => x.org.name)).toEqual(["Alpha", "beta", "Gamma"]);
    expect(g[1].sites.map((s) => s.name)).toEqual(["annex", "Yard"]);
    expect(g[1]).toMatchObject({ servers_online: 1, servers_total: 2, open_alerts: 2 });
    expect(g[0]).toMatchObject({ servers_online: 1, servers_total: 2, open_alerts: 1 });
  });
  it("keeps an empty customer unless filtering", () => {
    expect(groupByCustomer(rows).find((x) => x.org.id === "o_c")?.sites).toEqual([]);
    expect(groupByCustomer(rows, "yard").map((x) => x.org.id)).toEqual(["o_b"]);
  });
  it("filters on Site name or address, or keeps every Site of a matching customer", () => {
    expect(groupByCustomer(rows, "main st")[0].sites.map((s) => s.id)).toEqual(["l_hq"]);
    expect(groupByCustomer(rows, "main st")[0].unassigned).toEqual([]);
    expect(groupByCustomer(rows, "BETA")[0].sites.map((s) => s.id)).toEqual(["l_a", "l_y"]);
  });
  it("leaves retired servers out of the counts", () => {
    const r: HubSitesOrg[] = [{ org: { id: "o", name: "O" }, locations: [site("l", "L", [srv("a", true), srv("b", false, { retired_at: 1 })])], unassigned: [] }];
    expect(groupByCustomer(r)[0]).toMatchObject({ servers_online: 1, servers_total: 1 });
  });
});
