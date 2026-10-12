import { describe, expect, it } from "vitest";
import type { Server, ServerCamera, ServerSummary } from "./api";
import {
  NO_FILTERS, UNASSIGNED, aiText, anyAction, cameraCounts, compareVersions, daysLeft, downText, filterServers, groupBySite, hasProblems,
  latestVersion, matchesSearch, networkText, parseFilters, recordingText, serverActions, updateAvailable,
} from "./serverList";

const cam = (name: string, ok = true, problems: string[] = []): ServerCamera => ({ id: name.toLowerCase(), name, stream_ready: ok, problems });
const srv = (over: Partial<Server> = {}, summary: ServerSummary = {}): Server => ({
  id: over.name ?? "s1", org_id: "o", name: "Main NVR", location: "", online: true, last_seen_at: 1, version: "0.9.0", hostname: null,
  clock_skew_s: null, open_alerts: 0, summary, ...over,
});
const central = { id: "c1", mode: "vpn" as const, quota_gb: 2000, used_gb: 186, site_number: 7, state: "running" };

describe("serverActions", () => {
  it("gives an admin every action on an ordinary server, Move only when there is another Site", () => {
    expect(serverActions(srv(), true, 2)).toEqual({ rename: true, note: true, move: true, backups: true, rotate: true, retire: "retire", remove: true });
    expect(serverActions(srv(), true, 0).move).toBe(false);
    expect(serverActions(srv({ retired_at: 5 }), true, 1).retire).toBe("restore");
  });
  it("keeps a central server to Rename, Note and Backups (Restore when retired)", () => {
    expect(serverActions(srv({ central }), true, 3)).toEqual({ rename: true, note: true, move: false, backups: true, rotate: false, retire: null, remove: false });
    expect(serverActions(srv({ central, retired_at: 5 }), true, 3).retire).toBe("restore");
  });
  it("gives viewers nothing (only the console link)", () => {
    const a = serverActions(srv(), false, 3);
    expect(anyAction(a)).toBe(false);
    expect(anyAction(serverActions(srv({ central }), true, 0))).toBe(true);
  });
});

describe("cameras", () => {
  it("counts up and names the ones down (no stream or a problem)", () => {
    const s = srv({}, { cameras: [cam("Gate"), cam("Speaker Back Wall", false), cam("Dock", true, ["no motion data"])] });
    expect(cameraCounts(s)).toEqual({ up: 1, total: 3, down: ["Speaker Back Wall", "Dock"] });
  });
  it("shortens a long list with +N", () => {
    expect(downText([])).toBeNull();
    expect(downText(["Speaker Back Wall"])).toBe("1 down: Speaker Back Wall");
    expect(downText(["A", "B", "C", "D"])).toBe("4 down: A, B +2");
  });
});

describe("recording, AI and network text", () => {
  it("shows free disk in TB from 1000 GB and how long it lasts at the current bitrate", () => {
    // 10 Mbit/s = 108 GB a day
    const s = srv({}, { disk: { free_gb: 7100, total_gb: 8000 }, bitrate_mbps: 14.3 });
    expect(daysLeft(s)).toBe(46);
    expect(recordingText(s)).toBe("7.1 TB free · room for ~46 days");
    expect(recordingText(srv({}, { disk: { free_gb: 812, total_gb: 1000 } }))).toBe("812 GB free");
    expect(recordingText(srv())).toBe("—");
  });
  it("shows a central instance's storage quota instead", () => {
    expect(recordingText(srv({ central }, { disk: { free_gb: 50, total_gb: 100 } }))).toBe("186 GB of 2.0 TB (9%)");
  });
  it("says whether YOLO and Qwen are ready, and what waits for a synopsis", () => {
    expect(aiText(srv({}, { yolo_ready: true, vlm_ready: false }))).toBe("YOLO ✓ · Qwen ✗");
    expect(aiText(srv({}, { yolo_ready: true, vlm_ready: true, queues: { verify: 0, synopsis: 3 } }))).toBe("YOLO ✓ · Qwen ✓ (3 waiting)");
  });
  it("shows what the cameras send, or nothing", () => {
    expect(networkText(srv({}, { bandwidth: { mbps: 16.8, month_gb: 4.7 } }))).toBe("16.8 Mbit/s in · 4.7 GB this month");
    expect(networkText(srv())).toBeNull();
  });
});

describe("hasProblems", () => {
  it("is offline, a camera down or open alerts, never a retired server", () => {
    expect(hasProblems(srv({}, { cameras: [cam("Gate")] }))).toBe(false);
    expect(hasProblems(srv({ online: false }))).toBe(true);
    expect(hasProblems(srv({ open_alerts: 2 }))).toBe(true);
    expect(hasProblems(srv({}, { cameras: [cam("Gate", false)] }))).toBe(true);
    expect(hasProblems(srv({ online: false, retired_at: 9 }))).toBe(false);
  });
});

describe("versions", () => {
  it("compares part by part, numerically", () => {
    expect(compareVersions("0.10.0", "0.9.3")).toBe(1);
    expect(compareVersions("0.9.0", "0.9")).toBe(0);
    expect(compareVersions("v1.2.0", "1.2.1")).toBe(-1);
  });
  it("flags servers below the newest one in service", () => {
    const servers = [srv({ version: "0.9.0" }), srv({ version: "0.10.1" }), srv({ version: "0.11.0", retired_at: 3 }), srv({ version: null })];
    const latest = latestVersion(servers);
    expect(latest).toBe("0.10.1");
    expect(updateAvailable(servers[0], latest)).toBe(true);
    expect(updateAvailable(servers[1], latest)).toBe(false);
    expect(updateAvailable(servers[3], latest)).toBe(false);
    expect(latestVersion([])).toBeNull();
  });
});

describe("search and filters", () => {
  const a = srv({ id: "a", name: "Main NVR", location: "Rack 2", hostname: "nvr-main", location_id: "L1", location_name: "Main Facility" }, { cameras: [cam("Speaker Back Wall")] });
  const b = srv({ id: "b", name: "Yard box", online: false, location_id: "L2", location_name: "Yard" });
  const c = srv({ id: "c", name: "Spare", location_id: null });
  it("matches name, note, host, Site and camera names, every word", () => {
    for (const q of ["main", "rack 2", "NVR-MAIN", "facility", "speaker wall", ""]) expect(matchesSearch(a, q), q).toBe(true);
    expect(matchesSearch(a, "yard")).toBe(false);
  });
  it("filters by Site, Unassigned and problems", () => {
    expect(filterServers([a, b, c], { ...NO_FILTERS, site: "L2" }).map((s) => s.id)).toEqual(["b"]);
    expect(filterServers([a, b, c], { ...NO_FILTERS, site: UNASSIGNED }).map((s) => s.id)).toEqual(["c"]);
    expect(filterServers([a, b, c], { ...NO_FILTERS, problems: true }).map((s) => s.id)).toEqual(["b"]);
    expect(filterServers([a, b, c], { q: "spare", site: "", problems: false }).map((s) => s.id)).toEqual(["c"]);
  });
  it("reads saved filters defensively", () => {
    expect(parseFilters(JSON.stringify({ q: "gate", site: "L1", problems: true }))).toEqual({ q: "gate", site: "L1", problems: true });
    expect(parseFilters("{not json")).toEqual(NO_FILTERS);
    expect(parseFilters(null)).toEqual(NO_FILTERS);
    expect(parseFilters(JSON.stringify({ q: 3, problems: "yes" }))).toEqual(NO_FILTERS);
  });
});

describe("groupBySite", () => {
  it("groups by Site name, servers by name, then unassigned; retired apart", () => {
    const servers = [
      srv({ id: "1", name: "NVR 10", location_id: "L1", location_name: "Main Facility" }),
      srv({ id: "2", name: "NVR 9", location_id: "L1", location_name: "Main Facility" }),
      srv({ id: "3", name: "Box", location_id: "L2", location_name: "Austin" }),
      srv({ id: "4", name: "Loose", location_id: null }),
      srv({ id: "5", name: "Old", location_id: "L1", location_name: "Main Facility", retired_at: 7 }),
      srv({ id: "6", name: "Nameless site", location_id: "L3" }),
    ];
    const g = groupBySite(servers, { L3: "Zed" });
    expect(g.groups.map((x) => [x.name, x.servers.map((s) => s.id)])).toEqual([["Austin", ["3"]], ["Main Facility", ["2", "1"]], ["Zed", ["6"]]]);
    expect(g.unassigned.map((s) => s.id)).toEqual(["4"]);
    expect(g.retired.map((s) => s.id)).toEqual(["5"]);
  });
});
