import { describe, expect, it } from "vitest";
import type { BridgeTarget } from "../api";
import {
  DEFAULT_BRIDGE_PORTS, bridgeStatus, fmtBytes, fmtUptime, installCommand, isLanAddress, mapFormError, oneLineInstall, parsePorts, portsText, probeText,
  relaySheet, sheetText, targetErrorText,
} from "./bridges";

const target = (ports: { port: number; listen?: number }[]): Pick<BridgeTarget, "ports"> => ({ ports: ports.map((p) => ({ label: null, ...p })) });

describe("installCommand (hub bridges.install_command prints the same)", () => {
  it("is the exe, the hub's /bridge URL and the one-time code", () => {
    expect(installCommand("wss://hub.axiomvision.ai/bridge", "AXB-ABCD-EFGH-JKLM"))
      .toBe(".\\axiom-bridge.exe install --hub wss://hub.axiomvision.ai/bridge --code AXB-ABCD-EFGH-JKLM");
  });
});

describe("oneLineInstall (download from the hub, check the SHA-256, install)", () => {
  const sha = "1c713eba5a184f00d6b8a0e4add3f9f656170104f2e8cc89258de466c33218b4";
  const rel = { version: "0.1.0", windows: { file: "0.1.0/axiom-bridge.exe", sha256: sha } };
  it("downloads that version from this hub, refuses a wrong checksum, then installs with the code", () => {
    const c = oneLineInstall("https://hub.axiomvision.ai/", "wss://hub.axiomvision.ai/bridge", "AXB-ABCD-EFGH-JKLM", rel)!;
    expect(c).toContain("[Net.ServicePointManager]::SecurityProtocol = 'Tls12'");
    expect(c).toContain("iwr -UseBasicParsing https://hub.axiomvision.ai/downloads/bridge/0.1.0/axiom-bridge.exe -OutFile $f");
    expect(c).toContain(`-ne '${sha.toUpperCase()}') { Remove-Item $f; throw`);
    expect(c.endsWith("& $f install --hub wss://hub.axiomvision.ai/bridge --code AXB-ABCD-EFGH-JKLM")).toBe(true);
    expect(c).not.toContain("\n");
  });
  it("is null without a usable release (none published, a bad hash or file name)", () => {
    expect(oneLineInstall("https://h", "wss://h/bridge", "X", null)).toBeNull();
    expect(oneLineInstall("https://h", "wss://h/bridge", "X", { version: "1" })).toBeNull();
    expect(oneLineInstall("https://h", "wss://h/bridge", "X", { version: "1", windows: { file: "a.exe", sha256: "abc" } })).toBeNull();
    expect(oneLineInstall("https://h", "wss://h/bridge", "X", { version: "1", windows: { file: "a.exe; calc", sha256: sha } })).toBeNull();
  });
});

describe("relaySheet: what Apply to camera writes, for entering by hand", () => {
  const t = target([{ port: 554, listen: 47000 }, { port: 80, listen: 47001 }, { port: 555, listen: 47002 }]);
  it("the gateway and the relay port of RTSP, ONVIF and SD replay", () => {
    expect(relaySheet(t, "10.200.0.1")).toEqual([
      { field: "public_host", label: "Public host", value: "10.200.0.1" },
      { field: "public_rtsp_port", label: "Public RTSP port", value: "47000" },
      { field: "public_onvif_port", label: "Public ONVIF port", value: "47001" },
      { field: "public_replay_port", label: "Public replay (SD) port", value: "47002" },
    ]);
    expect(sheetText(relaySheet(t, "10.200.0.1"))).toBe("Public host: 10.200.0.1\nPublic RTSP port: 47000\nPublic ONVIF port: 47001\nPublic replay (SD) port: 47002");
  });
  it("follows the camera's own RTSP / ONVIF ports and lists the other bridged ones", () => {
    const odd = target([{ port: 8554, listen: 47010 }, { port: 8000, listen: 47011 }, { port: 443, listen: 47012 }]);
    expect(relaySheet(odd, "10.200.0.17", { rtsp_port: 8554, onvif_port: 8000 }).map((r) => [r.field, r.value])).toEqual([
      ["public_host", "10.200.0.17"], ["public_rtsp_port", "47010"], ["public_onvif_port", "47011"], ["port_443", "47012"],
    ]);
  });
  it("leaves out what the bridge does not carry, and says when the gateway is unknown", () => {
    const rows = relaySheet(target([{ port: 554, listen: 47000 }]), null);
    expect(rows.map((r) => r.field)).toEqual(["public_host", "public_rtsp_port"]);
    expect(rows[0].value).toMatch(/not reported yet/);
    expect(relaySheet(target([{ port: 554 }]), "10.200.0.1").length).toBe(1);   // no listen ports (not an admin's view)
  });
});

describe("status lines", () => {
  it("bridgeStatus", () => {
    expect(bridgeStatus({ online: true, streams: 2, uptime_s: 3 * 3600 + 240, last_seen_at: 1 })).toBe("online · 2 streams · up 3 h 4 min");
    expect(bridgeStatus({ online: true, streams: 1, uptime_s: null, last_seen_at: 1 })).toBe("online · 1 stream");
    expect(bridgeStatus({ online: false, streams: 0, uptime_s: null, last_seen_at: 1000 }, 1000 + 720)).toBe("offline · last seen 12 min ago");
    expect(bridgeStatus({ online: false, streams: 0, uptime_s: null, last_seen_at: null })).toBe("never connected");
  });
  it("fmtBytes and fmtUptime", () => {
    expect([fmtBytes(512), fmtBytes(1_300_000), fmtBytes(4.2e12), fmtBytes(null)]).toEqual(["512 B", "1.3 MB", "4.2 TB", "—"]);
    expect([fmtUptime(45), fmtUptime(7260), fmtUptime(2 * 86400 + 3600), fmtUptime(undefined)]).toEqual(["45 s", "2 h 1 min", "2 d 1 h", "—"]);
  });
  it("probeText, targetErrorText and portsText", () => {
    expect(probeText({ at: 1, ports: [{ port: 554, ok: true, ms: 3.2, error: null }, { port: 555, ok: false, ms: null, error: "connection refused" }] }))
      .toBe("554 ✓ 3 ms · 555 ✗ connection refused");
    expect(probeText(null)).toBeNull();
    expect(targetErrorText({ errors: { "554": "connection refused" } })).toBe("554: connection refused");
    expect(targetErrorText({ errors: {} })).toBeNull();
    expect(portsText([{ port: 554, label: "RTSP", listen: 47000 }, { port: 80, label: "ONVIF", listen: 47001 }])).toBe("554 → 47000 · 80 → 47001");
    expect(portsText([{ port: 554, label: "RTSP" }, { port: 8000, label: null }])).toBe("554 RTSP · 8000");
  });
});

describe("the Map a camera form (the hub checks the same)", () => {
  it("parsePorts", () => {
    expect(parsePorts("")).toEqual({ ports: DEFAULT_BRIDGE_PORTS, error: null });
    expect(parsePorts("554, 80 555")).toEqual({ ports: [554, 80, 555], error: null });
    expect(parsePorts("554, 554").error).toMatch(/twice/);
    expect(parsePorts("0").error).toMatch(/not a TCP port/);
    expect(parsePorts("70000").error).toMatch(/not a TCP port/);
    expect(parsePorts("1 2 3 4 5 6 7 8 9").error).toMatch(/at most 8/);
  });
  it("isLanAddress: RFC 1918 only, like the bridge", () => {
    for (const a of ["10.0.0.5", "172.16.1.1", "172.31.255.254", "192.168.1.50"]) expect(isLanAddress(a)).toBe(true);
    for (const a of ["8.8.8.8", "172.32.0.1", "172.15.0.1", "100.64.1.1", "127.0.0.1", "192.169.1.1", "192.168.1.256", "cam.local", ""]) expect(isLanAddress(a)).toBe(false);
  });
  it("mapFormError", () => {
    const ok = { name: "Gate", host: "192.168.1.50", ports: "554, 80, 555" };
    expect(mapFormError(ok)).toBeNull();
    expect(mapFormError({ ...ok, name: " " })).toMatch(/name/);
    expect(mapFormError({ ...ok, host: "8.8.8.8" })).toMatch(/private IPv4/);
    expect(mapFormError(ok, ["192.168.1.50/24"])).toMatch(/bridge PC itself/);
    expect(mapFormError({ ...ok, ports: "abc" })).toMatch(/not a TCP port/);
  });
});
