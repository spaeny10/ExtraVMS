import { describe, expect, it } from "vitest";
import type { CentralInstance, FleetSummary } from "../api";
import {
  NO_QUERY, PAGE_SIZE, activeAttention, attentionItems, bulkPlan, bulkValueError, camerasDown, defaultOrder, downText, hostMiniBars, hostParams, imageTitle,
  instanceParams, lastPage, pageText, parseQuery, querySearch, rowProblems, shortImageId, tabQuery, withQuery,
} from "./fleetList";

const summary = (over: { hosts?: Partial<FleetSummary["hosts"]>; instances?: Partial<FleetSummary["instances"]> } & Partial<Omit<FleetSummary, "hosts" | "instances">> = {}): FleetSummary => ({
  cameras_down: 0, newest_agent: "0.3.1", images_reported: true, healthy: false, host_index: [],
  ...over,
  hosts: { total: 4, online: 4, offline: 0, agent: 0, queue: 0, ...over.hosts },
  instances: { total: 20, any: 0, down: 0, storage: 0, queue: 0, image: 0, cameras: 0, ...over.instances },
});

const ci = (over: Partial<CentralInstance> = {}): CentralInstance => ({
  id: "ci_1", location_id: "l_1", location_name: "Yard", org_id: "o_1", org_name: "Acme", server_id: "s_1", server_online: true, name: "Central",
  mode: "vpn", subnet: null, public_ip: null, site_number: 1, quota_gb: 1000, used_gb: 100, state: "running", phase: "running", created_at: 1,
  ready_at: 1, info_at: 1, peplink: {} as CentralInstance["peplink"], host_online: true, ...over,
});

describe("the URL query", () => {
  it("round-trips, dropping defaults", () => {
    expect(querySearch(NO_QUERY)).toBe("");
    const q = { ...tabQuery("instances"), q: "yard", problem: "storage", sort: "storage", order: "asc" as const, page: 3, host: "h_1" };
    expect(querySearch(q)).toBe("?tab=instances&q=yard&host=h_1&problem=storage&sort=storage&order=asc&page=3");
    expect(parseQuery(querySearch(q))).toEqual(q);
    const h = { ...tabQuery("hosts"), status: "offline", sort: "free" };
    expect(parseQuery(querySearch(h))).toEqual(h);
  });
  it("ignores what doesn't belong to the tab, and junk", () => {
    expect(parseQuery("?tab=hosts&problem=storage&state=failed&sort=storage")).toEqual(NO_QUERY);   // instance filters on the Hosts tab
    expect(parseQuery("?tab=instances&status=offline").status).toBe("");
    expect(parseQuery("?tab=nope&page=-2&order=sideways&problem=evil")).toEqual(NO_QUERY);
    expect(parseQuery("?page=2.7").page).toBe(2);
  });
  it("a host's page has one list: the tab is fixed and stays out of the URL", () => {
    const q = parseQuery("?problem=down&tab=hosts", "instances");
    expect(q.tab).toBe("instances");
    expect(q.problem).toBe("down");
    expect(querySearch(q, "instances")).toBe("?problem=down");
  });
  it("a new filter goes back to page 1", () => {
    expect(withQuery({ ...NO_QUERY, page: 4 }, { q: "x" }).page).toBe(1);
    expect(withQuery({ ...NO_QUERY, page: 4 }, { page: 5 }).page).toBe(5);
  });
});

describe("API parameters", () => {
  it("pages and passes only what is set", () => {
    expect(hostParams({ ...NO_QUERY, page: 2, status: "offline" })).toEqual({ q: undefined, status: "offline", sort: undefined, order: undefined, limit: PAGE_SIZE, offset: PAGE_SIZE });
    const p = instanceParams({ ...tabQuery("instances"), host: "h_other", mode: "bridge" }, "h_mine");
    expect(p).toMatchObject({ host: "h_mine", mode: "bridge", limit: PAGE_SIZE, offset: 0 });
    expect(instanceParams(tabQuery("instances")).limit).toBe(PAGE_SIZE);   // always paged: no limit means the old unpaged list
  });
  it("page text", () => {
    expect(pageText(1, 50, 120)).toBe("1–50 of 120");
    expect(pageText(3, 20, 120)).toBe("101–120 of 120");
    expect(pageText(1, 0, 0)).toBe("None");
    expect(pageText(9, 0, 120)).toBe("None on page 9 of 120");
    expect(lastPage(0)).toBe(1);
    expect(lastPage(101)).toBe(3);
  });
  it("default sort orders", () => {
    expect(defaultOrder("storage")).toBe("desc");
    expect(defaultOrder("site")).toBe("asc");
    expect(defaultOrder("name")).toBe("asc");
    expect(defaultOrder("free")).toBe("desc");
  });
});

describe("attention strip", () => {
  it("nothing when all is well", () => {
    expect(attentionItems(summary({ healthy: true }))).toEqual([]);
    expect(attentionItems(null)).toEqual([]);
  });
  it("worst first, each opening its list", () => {
    const items = attentionItems(summary({ hosts: { offline: 1, agent: 2 }, instances: { down: 3, storage: 1, queue: 2, image: 4, cameras: 2 }, cameras_down: 5 }));
    expect(items.map((i) => i.key)).toEqual(["offline", "down", "storage", "queue", "agent", "image", "cameras"]);
    expect(items.map((i) => i.text)).toEqual(["1 host offline", "3 instances not running", "1 instance at 90 % storage or more", "2 detection queues backing up",
      "2 hosts on an older agent", "4 instances on another image", "5 cameras down at 2 instances"]);
    expect(items[0].tone).toBe("bad");
    expect(items[0].to).toMatchObject({ tab: "hosts", status: "offline" });
    expect(items[2].to).toMatchObject({ tab: "instances", problem: "storage", sort: "storage" });
    expect(items[4].title).toContain("0.3.1");
  });
  it("lights the line whose filter is shown", () => {
    const [off, down] = attentionItems(summary({ hosts: { offline: 1 }, instances: { down: 1 } }));
    expect(activeAttention(off, parseQuery(querySearch(off.to)))).toBe(true);
    expect(activeAttention(down, parseQuery("?tab=instances&problem=down&q=yard"))).toBe(true);
    expect(activeAttention(down, parseQuery("?tab=instances"))).toBe(false);
    expect(activeAttention(off, parseQuery("?tab=instances&problem=down"))).toBe(false);
  });
});

describe("rows", () => {
  it("host mini bars: CPU, RAM, the busiest GPU, the fullest disk", () => {
    const b = hostMiniBars({ cpus: 10, load: [5, 4, 3], ram_gb: { total: 100, free: 25 },
      gpus: [{ index: 0, name: "NVIDIA A40", mem_total_mb: 1000, mem_used_mb: 900 }, { index: 1, name: "NVIDIA A10", mem_total_mb: 1000, mem_used_mb: 100 }],
      disks: [{ path: "/srv", total_gb: 100, free_gb: 50 }] });
    expect(b.cpu?.pct).toBe(50);
    expect(b.ram?.pct).toBe(75);
    expect(b.gpu?.pct).toBe(90);
    expect(b.gpu?.text).toContain("GPU 0 · A40: ");
    expect(b.gpu?.text.split("\n")).toHaveLength(2);
    expect(b.disk?.pct).toBe(50);
    expect(hostMiniBars(null)).toEqual({ cpu: null, ram: null, gpu: null, disk: null });
  });
  it("cameras down, why it is down, the row's badges", () => {
    expect(camerasDown(ci({ camera_count: 5, cameras_online: 3, problems: ["cameras"] }))).toBe(2);
    expect(camerasDown(ci({ camera_count: 5, cameras_online: 0, server_online: false, problems: [] }))).toBe(0);
    expect(downText(ci({ problems: ["down"], host_state: "exited" }))).toBe("Container exited");
    expect(downText(ci({ problems: ["down"], host_state: "running", server_online: false }))).toBe("Server not connected");
    expect(downText(ci({ problems: ["down"], state: "failed" }))).toBeNull();   // its State says Failed already
    expect(downText(ci({ problems: [] }))).toBeNull();
    expect(rowProblems(ci({ problems: ["storage", "queue", "cameras", "image"] }))).toEqual(["queue", "image"]);
    const a = `sha256:${"4f1c2a9e0b7d".padEnd(64, "0")}`, b = `sha256:${"9d00e1".padEnd(64, "1")}`;
    expect(imageTitle({ image: "axiom/instance:2096ed5", image_id: a, host_image: "axiom/instance:latest", host_image_id: b }))
      .toBe("Runs axiom/instance:2096ed5 (4f1c2a9e0b7d); its host creates axiom/instance:latest (9d00e1111111)");
    expect(imageTitle({ image: null, image_id: null, host_image: null, host_image_id: null })).toBe("Runs ?; its host creates ?");
    expect(shortImageId(null)).toBeNull();
  });
});

describe("bulk changes", () => {
  const a = ci({ id: "a", quota_gb: 500 }), b = ci({ id: "b", quota_gb: 2000 }), c = ci({ id: "c", state: "deleting" }), d = ci({ id: "d", host_online: false, quota_gb: 100 });
  it("camera limit: every live one", () => {
    const p = bulkPlan("limit", [a, b, c, d]);
    expect(p.apply.map((x) => x.id)).toEqual(["a", "b", "d"]);
    expect(p.skip).toEqual([{ ci: c, why: "being removed" }]);
  });
  it("storage is only raised, and needs the host", () => {
    const p = bulkPlan("storage", [a, b, c, d], 1000);
    expect(p.apply.map((x) => x.id)).toEqual(["a"]);
    expect(p.skip.map((s) => [s.ci.id, s.why])).toEqual([["b", "already 2.0 TB"], ["c", "being removed"], ["d", "its host is offline"]]);
    expect(bulkValueError("storage", "9")).not.toBeNull();
    expect(bulkValueError("storage", "1.5")).not.toBeNull();
    expect(bulkValueError("storage", "10")).toBeNull();
  });
});
