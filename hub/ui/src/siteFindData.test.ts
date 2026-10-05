import { describe, expect, it } from "vitest";
import type { Camera } from "@site/api";
import { hubEventKey, hubFindParams, newerAcross, siteFindCameras, siteFindHandoff } from "./siteFindData";

const cam = (id: string, name: string, zones: Camera["zones"] = []) => ({ id, name, zones }) as unknown as Camera;

describe("siteFindCameras", () => {
  const servers = [{ id: "s1", name: "Gate box" }, { id: "s2", name: "Yard box" }];
  it("keys cameras by server and names them Server · Camera when the Site has several servers", () => {
    const out = siteFindCameras(servers, { s1: [cam("cam1", "Front")], s2: [cam("cam1", "Back")] });
    expect(out.map((c) => [c.id, c.name])).toEqual([["s1/cam1", "Gate box · Front"], ["s2/cam1", "Yard box · Back"]]);
  });
  it("just the camera on a one-server Site; a server not loaded yet adds nothing; zones kept", () => {
    const z = [{ name: "Dock", type: "area" }] as unknown as Camera["zones"];
    const out = siteFindCameras([servers[0]], { s1: [cam("cam1", "Front", z)] });
    expect(out.map((c) => c.name)).toEqual(["Front"]);
    expect(out[0].zones).toBe(z);
    expect(siteFindCameras(servers, { s1: [cam("c", "C")] })).toHaveLength(1);
  });
});

describe("hubFindParams", () => {
  it("lane key → server:camera, flags joined, attention only when set, cursor and limit passed", () => {
    expect(hubFindParams({ camera: "s1/cam/2", flags: ["rule", "ppe"], attention: true, status: "verified", sort: "priority", offset: 5 }, "{\"s1\":{}}", 60))
      .toEqual({ camera: "s1:cam/2", flags: "rule,ppe", attention: true, status: "verified", sort: "priority", limit: 60, cursor: "{\"s1\":{}}" });
    expect(hubFindParams({ attention: false, flags: [] }, null, 10)).toEqual({ camera: undefined, flags: undefined, attention: undefined, limit: 10, cursor: undefined });
  });
});

describe("keys and order", () => {
  it("the same id on two servers is two events", () => {
    expect(hubEventKey({ site_id: "s1", id: 4 })).not.toBe(hubEventKey({ site_id: "s2", id: 4 }));
  });
  it("newest start first across servers, then id", () => {
    const list = [{ start_ts: 10, id: 9 }, { start_ts: 30, id: 1 }, { start_ts: 10, id: 12 }].sort(newerAcross);
    expect(list.map((e) => e.id)).toEqual([1, 12, 9]);
  });
});

describe("siteFindHandoff", () => {
  it("a bare ?q= is a question to ask; FindView's own ?view=…&q= is a search", () => {
    expect(siteFindHandoff("?q=Anything%20unusual%3F")).toBe("Anything unusual?");
    expect(siteFindHandoff("?view=attention&q=truck")).toBeNull();
    expect(siteFindHandoff("")).toBeNull();
    expect(siteFindHandoff("?q=%20")).toBeNull();
  });
});
