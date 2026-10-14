import { describe, expect, it } from "vitest";
import type { SummarySpeaker } from "../api";
import { cameraNames, camerasByServer, membersText, talkByCamera, toggleKeys } from "./cameraGroups";

const srv = (id: string, name: string, speakers: SummarySpeaker[] = [], retired_at: number | null = null) => ({ id, name, summary: { speakers }, retired_at });
const client = (server: string) => `client:${server}`;

describe("which speakers talk on a camera (direct bindings plus camera groups)", () => {
  const alpha = srv("s_a", "Alpha", [
    { id: "spk1", name: "Back wall", camera_ids: ["cam2"], online: true },
    { id: "spk2", name: "Gate horn", camera_ids: [] },
  ]);
  const bravo = srv("s_b", "Bravo", [{ id: "spk1", name: "Lot", camera_ids: ["cam3"], online: false }]);
  const groups = [
    { id: 1, cameras: ["s_a/cam1", "s_b/cam1"], speakers: [{ server_id: "s_a", speaker_id: "spk2" }] },
    { id: 2, cameras: ["s_b/cam3", "s_a/cam2"], speakers: [{ server_id: "s_a", speaker_id: "spk2" }, { server_id: "s_a", speaker_id: "spk1" }] },
    { id: 3, cameras: ["s_b/cam9"], speakers: [] },
  ];

  it("a server's own bindings only, without groups (one server: plain names)", () => {
    const m = talkByCamera([alpha], [], client);
    expect(Object.keys(m)).toEqual(["s_a/cam2"]);
    expect(m["s_a/cam2"]).toEqual([{ id: "spk1", name: "Back wall", online: true, server: "s_a", site: "client:s_a" }]);
  });
  it("a group's speaker talks on its cameras of every server, through its own server's client", () => {
    const m = talkByCamera([alpha, bravo], groups, client);
    // a camera of Bravo, covered by Alpha's speaker through a group: the panel calls Alpha
    expect(m["s_b/cam1"]).toEqual([{ id: "spk2", name: "Gate horn (Alpha)", online: undefined, server: "s_a", site: "client:s_a" }]);
    expect(m["s_b/cam3"].map((s) => [s.server, s.id, s.site])).toEqual([["s_a", "spk1", "client:s_a"], ["s_a", "spk2", "client:s_a"], ["s_b", "spk1", "client:s_b"]]);
    // bound directly and through a group: listed once
    expect(m["s_a/cam2"].map((s) => `${s.server}/${s.id}`)).toEqual(["s_a/spk1", "s_a/spk2"]);
    expect(m["s_b/cam9"]).toBeUndefined();   // a group without speakers
    expect(m["s_a/cam1"].map((s) => s.name)).toEqual(["Gate horn (Alpha)"]);
  });
  it("membership changes follow at once; retired servers and disabled speakers are left out", () => {
    const moved = [{ ...groups[0], cameras: ["s_b/cam9"] }];
    const m = talkByCamera([alpha, bravo], moved, client);
    expect(m["s_a/cam1"]).toBeUndefined();
    expect(m["s_b/cam9"].map((s) => s.id)).toEqual(["spk2"]);
    expect(talkByCamera([alpha, srv("s_b", "Bravo", bravo.summary.speakers, 1)], [], client)["s_b/cam3"]).toBeUndefined();
    const off = srv("s_c", "Charlie", [{ id: "x", name: "Off", camera_ids: ["c1"], enabled: false } as SummarySpeaker]);
    expect(talkByCamera([off], [], client)).toEqual({});
    // a server without a speakers list (older server) and a group naming a speaker no server reports
    expect(talkByCamera([{ id: "s_d", name: "Old", summary: {} }], [{ id: 9, cameras: ["s_d/c1"], speakers: [{ server_id: "s_d", speaker_id: "gone" }] }], client)).toEqual({});
  });
});

describe("the members checklist", () => {
  const servers = [srv("s_b", "Bravo"), srv("s_a", "alpha"), srv("s_r", "Retired", [], 5)];
  const registry = [
    { server_id: "s_a", camera_id: "cam10", name: "Gate 10", missing_since: null },
    { server_id: "s_a", camera_id: "cam2", name: "Gate 2", missing_since: null },
    { server_id: "s_a", camera_id: "old", name: "Old", missing_since: 100 },
    { server_id: "s_b", camera_id: "cam1", name: "", missing_since: null },
    { server_id: "s_r", camera_id: "cam1", name: "Retired cam", missing_since: null },
  ];
  it("cameras by server (by name), gone ones only while a group holds them", () => {
    const c = camerasByServer(servers, registry);
    expect(c.map((s) => s.name)).toEqual(["alpha", "Bravo"]);
    expect(c[0].cameras.map((x) => x.key)).toEqual(["s_a/cam2", "s_a/cam10"]);
    expect(c[1].cameras).toEqual([{ key: "s_b/cam1", name: "cam1", missing: false }]);
    expect(camerasByServer(servers, registry, ["s_a/old"])[0].cameras.map((x) => [x.key, x.missing])).toContainEqual(["s_a/old", true]);
    expect(cameraNames(["s_a/cam2", "s_b/cam1", "s_x/zz"], c, true)).toEqual(["alpha · Gate 2", "Bravo · cam1", "s_x/zz"]);
    expect(cameraNames(["s_a/cam2"], c, false)).toEqual(["Gate 2"]);
  });
  it("turns cameras on and off, keeping the order they were added in", () => {
    expect(toggleKeys(["b", "a"], ["c", "a"], true)).toEqual(["b", "a", "c"]);
    expect(toggleKeys(["b", "a", "c"], ["a", "x"], false)).toEqual(["b", "c"]);
  });
  it("says how many cameras on how many servers", () => {
    expect(membersText([])).toBe("No cameras");
    expect(membersText(["s_a/1"])).toBe("1 camera");
    expect(membersText(["s_a/1", "s_a/2"])).toBe("2 cameras");
    expect(membersText(["s_a/1", "s_b/2", "s_b/3"])).toBe("3 cameras on 2 servers");
  });
});
