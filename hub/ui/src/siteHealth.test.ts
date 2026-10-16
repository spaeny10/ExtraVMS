import { describe, expect, it } from "vitest";
import { type HealthInput, type ServerHealth, WEAK_CELLULAR, alertBadge, healthLabel, offersText, siteHealth } from "./siteHealth";

const srv = (over: Partial<ServerHealth> = {}): ServerHealth => ({ id: "a", name: "DC1 Server", online: true, route: "hub", offer: false, ...over });
const input = (over: Partial<HealthInput> = {}): HealthInput => ({
  servers: [srv({ id: "a" }), srv({ id: "b", name: "DC2" }), srv({ id: "c", name: "Gate" })],
  serversOnline: 3, serversTotal: 3, camerasOnline: 14, camerasTotal: 14, cellularScore: null, ...over,
});

describe("siteHealth", () => {
  it("all well: green, the counts, no note", () => {
    const h = siteHealth(input());
    expect(h.level).toBe("ok");
    expect(h.text).toBe("3/3 servers · 14/14 cameras");
    expect(h.short).toBe("14/14 cameras");
    expect(h.note).toBeNull();
    expect(h.reasons).toEqual([]);
    expect(healthLabel(h)).toBe("Site health: 3/3 servers · 14/14 cameras. All well. Show details");
  });
  it("a camera down is amber", () => {
    const h = siteHealth(input({ camerasOnline: 13 }));
    expect(h.level).toBe("warn");
    expect(h.text).toBe("3/3 servers · 13/14 cameras");
    expect(h.reasons).toEqual(["1 camera down"]);
    expect(h.note).toBeNull();   // the count already says it
  });
  it("a server offline is red, and the short text names the servers", () => {
    const h = siteHealth(input({ serversOnline: 2, camerasOnline: 9, servers: [srv(), srv({ id: "b", online: false }), srv({ id: "c" })] }));
    expect(h.level).toBe("bad");
    expect(h.short).toBe("2/3 servers");
    expect(h.reasons).toEqual(["1 server offline", "5 cameras down"]);
    expect(healthLabel(h)).toBe("Site health: 2/3 servers · 9/14 cameras. 1 server offline. 5 cameras down. Show details");
  });
  it("a server whose link to the cameras is down is amber, with a note", () => {
    const h = siteHealth(input({ servers: [srv({ linkDown: true }), srv({ id: "b", name: "DC2" })], serversOnline: 2, serversTotal: 2 }));
    expect(h.level).toBe("warn");
    expect(h.note).toBe("link down");
    expect(h.reasons[0]).toBe("link to the cameras down at DC1 Server");
    // an offline server's last heartbeat doesn't count
    expect(siteHealth(input({ servers: [srv({ online: false, linkDown: true })], serversOnline: 0, serversTotal: 1 })).note).toBeNull();
  });
  it("weak cellular is amber and shows on the pill; a fair or good score doesn't", () => {
    expect(siteHealth(input({ cellularScore: WEAK_CELLULAR - 0.1 }))).toMatchObject({ level: "warn", note: "weak cellular" });
    expect(siteHealth(input({ cellularScore: WEAK_CELLULAR }))).toMatchObject({ level: "ok", note: null });
    expect(siteHealth(input({ cellularScore: 10 }))).toMatchObject({ level: "ok", note: null });
    expect(siteHealth(input({ cellularScore: NaN })).level).toBe("ok");
  });
  it("red wins over amber; the link note wins over weak cellular", () => {
    const h = siteHealth(input({ serversOnline: 2, cellularScore: 1, servers: [srv({ linkDown: true }), srv({ id: "b", online: false })] }));
    expect(h.level).toBe("bad");
    expect(h.note).toBe("link down");
  });
  it("counts the direct-connection offers of online servers only", () => {
    const h = siteHealth(input({ servers: [srv({ route: "cert", offer: true }), srv({ id: "b", route: "cert", offer: true, online: false }), srv({ id: "c", route: "cert", offer: false })] }));
    expect(h.offers).toBe(1);
    expect(h.level).toBe("ok");   // an offer is not a problem
    expect(healthLabel(h)).toContain("1 server can connect directly.");
    expect(offersText(2)).toBe("2 servers can connect directly (faster)");
    expect(offersText(1)).toBe("1 server can connect directly (faster)");
    expect(offersText(0)).toBeNull();
  });
  it("singular and empty Sites read naturally", () => {
    expect(siteHealth(input({ servers: [srv()], serversOnline: 1, serversTotal: 1, camerasOnline: 1, camerasTotal: 1 })).text).toBe("1/1 server · 1/1 camera");
    const empty = siteHealth(input({ servers: [], serversOnline: 0, serversTotal: 0, camerasOnline: 0, camerasTotal: 0 }));
    expect(empty.text).toBe("No servers · no cameras");
    expect(empty.short).toBe("No servers");
    expect(empty.level).toBe("ok");
  });
});

describe("alertBadge", () => {
  it("shows the open count, nothing at zero, caps at 999+", () => {
    expect(alertBadge(61)).toBe("61");
    expect(alertBadge(1)).toBe("1");
    expect(alertBadge(0)).toBeNull();
    expect(alertBadge(null)).toBeNull();
    expect(alertBadge(undefined)).toBeNull();
    expect(alertBadge(-3)).toBeNull();
    expect(alertBadge(1000)).toBe("999+");
  });
});
