import { afterEach, describe, expect, it, vi } from "vitest";
import { type LiveReport, reportLive, setLiveReporter } from "@site/whepTiming";

const sent = vi.hoisted(() => [] as unknown[]);
vi.mock("./api", () => ({ sendLiveReport: (b: unknown) => { sent.push(b); } }));

import { installLiveTelemetry, liveBody, serverOfBase } from "./liveTelemetry";

const report = (over: Partial<LiveReport> = {}): LiveReport => ({
  event: "first_frame", base: "/s/s_abc", camera: "cam1", path: "cam1_sub", quality: "sd", route: "hub", attempt: 2,
  stages: { offer: 3, ice: 1500 }, ttff_ms: 1900, duration_s: null, ice_timed_out: true, ...over,
});

describe("serverOfBase", () => {
  it("reads the server id from a hub client's base", () => {
    expect(serverOfBase("/s/s_abc")).toBe("s_abc");
    expect(serverOfBase("")).toBeNull();
    expect(serverOfBase("https://192.168.1.5:8443")).toBeNull();
    expect(serverOfBase("/s/s_abc/x")).toBeNull();
  });
});

describe("liveBody", () => {
  it("is the hub's report shape, with the server id", () => {
    expect(liveBody(report())).toEqual({
      event: "first_frame", server: "s_abc", camera: "cam1", path: "cam1_sub", quality: "sd", route: "hub", attempt: 2,
      stages: { offer: 3, ice: 1500 }, ttff_ms: 1900, duration_s: null, ice_timed_out: true,
    });
    expect(liveBody(report({ route: "direct", event: "end", duration_s: 12.5 }))).toMatchObject({ route: "direct", event: "end", duration_s: 12.5 });
  });
  it("sends nothing for a client that names no server, or the NVR's own route", () => {
    expect(liveBody(report({ base: "" }))).toBeNull();
    expect(liveBody(report({ route: "local" }))).toBeNull();
  });
});

describe("installLiveTelemetry", () => {
  afterEach(() => { setLiveReporter(null); sent.length = 0; });
  it("sends the players' reports to the hub", () => {
    installLiveTelemetry();
    reportLive(report());
    reportLive(report({ base: "" }));
    expect(sent).toEqual([liveBody(report())]);
  });
});
