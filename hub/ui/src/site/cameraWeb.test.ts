import { describe, expect, it } from "vitest";
import { canOpenCameraWeb } from "../access";
import type { CameraWebSession, Me, Org } from "../api";
import { closesIn, openBlankTab, siteSessions, webCameras } from "./cameraWeb";

describe("who is offered Open camera web page (the hub checks the same, per Site)", () => {
  const me = (is_super = false): Me => ({ user: { id: "u", email: "e", totp_enabled: false, is_super }, orgs: [], active_org: null }) as Me;
  const org = (role: string, extra: Partial<Org> = {}): Org => ({ id: "o", name: "O", slug: "o", role, ...extra });
  it("the customer's admins and owners", () => {
    expect(canOpenCameraWeb(me(), org("admin"))).toBe(true);
    expect(canOpenCameraWeb(me(), org("owner", { member: true }))).toBe(true);
  });
  it("not viewers or operators", () => {
    expect(canOpenCameraWeb(me(), org("viewer"))).toBe(false);
    expect(canOpenCameraWeb(me(), org("operator"))).toBe(false);
  });
  it("not SOC staff widened into a customer they are not members of", () => {
    expect(canOpenCameraWeb(me(), org("admin", { soc: true }))).toBe(false);
    expect(canOpenCameraWeb(me(), org("admin", { soc: true, member: false }))).toBe(false);
    expect(canOpenCameraWeb(me(), org("operator", { soc: true }))).toBe(false);
    expect(canOpenCameraWeb(me(), org("admin", { soc: true, member: true }))).toBe(true);   // a real admin who is also SOC staff
  });
  it("hub administrators everywhere", () => {
    expect(canOpenCameraWeb(me(true), org("viewer"))).toBe(true);
    expect(canOpenCameraWeb(me(true), undefined)).toBe(true);
    expect(canOpenCameraWeb(me(), undefined)).toBe(false);
  });
});

describe("webCameras", () => {
  const servers = [{ id: "s_a", name: "Alpha", online: true, retired_at: null }, { id: "s_b", name: "Bravo", online: false, retired_at: null },
    { id: "s_r", name: "Old", online: true, retired_at: 5 }];
  const cam = (server_id: string, camera_id: string, name: string, extra = {}) =>
    ({ server_id, camera_id, name, enabled: true, missing_since: null, server_online: server_id === "s_a", ...extra });
  it("by server, by name; gone cameras and retired servers left out; disabled ones kept", () => {
    const out = webCameras(servers, [cam("s_a", "cam2", "Yard"), cam("s_a", "cam1", "Gate"), cam("s_a", "cam3", "Gone", { missing_since: 9 }),
      cam("s_b", "cam1", "Dock", { enabled: false }), cam("s_r", "cam1", "Retired")]);
    expect(out.map((s) => [s.id, s.online, s.cameras.map((c) => c.name)])).toEqual([["s_a", true, ["Gate", "Yard"]], ["s_b", false, ["Dock"]]]);
    expect(out[1].cameras[0].enabled).toBe(false);
  });
  it("a server without cameras isn't listed", () => {
    expect(webCameras(servers, [])).toEqual([]);
  });
});

describe("open sessions", () => {
  const s = (id: string, server_id: string, created_at: number) =>
    ({ id, server_id, created_at, expires_at: created_at + 1800 }) as CameraWebSession;
  it("this Site's, newest first", () => {
    expect(siteSessions([s("a", "s_a", 1), s("b", "s_x", 3), s("c", "s_a", 2)], ["s_a"]).map((x) => x.id)).toEqual(["c", "a"]);
  });
  it("closesIn", () => {
    expect(closesIn(100, 100)).toBe("closed");
    expect(closesIn(130, 100)).toBe("closes in under a minute");
    expect(closesIn(100 + 24 * 60, 100)).toBe("closes in 24 min");
    expect(closesIn(100 + 90 * 60, 100)).toBe("closes in 1 h 30 min");
  });
});

describe("openBlankTab", () => {
  it("opens about:blank and clears the opener, so the camera page can't touch the hub's tab", () => {
    const tab = { opener: {} as unknown } as Window;
    const calls: string[][] = [];
    expect(openBlankTab((u, t) => { calls.push([u, t]); return tab; })).toBe(tab);
    expect(calls).toEqual([["about:blank", "_blank"]]);
    expect(tab.opener).toBeNull();
  });
  it("null when the browser blocks it", () => {
    expect(openBlankTab(() => null)).toBeNull();
  });
});
