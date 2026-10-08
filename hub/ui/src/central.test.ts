import { describe, expect, it } from "vitest";
import {
  CENTRAL_TAG, addCamerasHint, addressParts, allocateFormError, cameraEntryError, cameraNetworkError, cameraNetworkOf, cameraNetworkSummary, camerasText, capacityBars,
  centralFormError, cleanCameraNetwork, defaultSubnet, fmtGB, fmtMbps, forwardCount, forwardRows, forwardTargets, isHostName, lanGateway,
  nextSiteNumber, overLimit, parseCameraLimit, parseIPv4, pct, phaseStep, quotaText, resolvedLines, settling, siteBandwidth, sitesWithoutCentral,
  connectionText, datacenterLine, instanceFor, limitText, reachableText, unenrolledInstances, uploadLine,
} from "./central";
import type { HubSitesOrg, ServerSummary, Site } from "./api";

const srv = (bandwidth?: ServerSummary["bandwidth"], retired = false) => ({ summary: { bandwidth } as ServerSummary, retired_at: retired ? 1 : null });

describe("site bandwidth", () => {
  it("sums the servers that report it and says so in one line", () => {
    const b = siteBandwidth([srv({ mbps: 12.1, today_gb: 40, month_gb: 300 }), srv({ mbps: 6.3, today_gb: 10, month_gb: 112 }), srv(undefined)]);
    expect(b).toEqual({ mbps: 18.4, today_gb: 50, month_gb: 412, servers: 2 });
    expect(uploadLine(b)).toBe("Upload from site: 18.4 Mbit/s · 412 GB this month");
  });
  it("leaves retired servers out; nothing reported = no line", () => {
    expect(siteBandwidth([srv({ mbps: 5, month_gb: 1 }, true)])).toBeNull();
    expect(uploadLine(siteBandwidth([srv(undefined)]))).toBeNull();
  });
});

describe("formatting", () => {
  it("GB and TB, Mbit/s", () => {
    expect(fmtGB(812)).toBe("812 GB");
    expect(fmtGB(4000)).toBe("4.0 TB");
    expect(fmtGB(12.34)).toBe("12.3 GB");
    expect(fmtGB(null)).toBe("—");
    expect(fmtMbps(18.44)).toBe("18.4 Mbit/s");
    expect(fmtMbps(250)).toBe("250 Mbit/s");
  });
  it("quota used of total", () => {
    expect(quotaText(812, 4000)).toBe("812 GB of 4.0 TB (20%)");
    expect(quotaText(null, 4000)).toBe("4.0 TB quota");
    expect(pct(5000, 4000)).toBe(100);
    expect(pct(1, 0)).toBeNull();
  });
});

describe("port-forward sheet", () => {
  it("camera k → RTSP 5540+k and ONVIF 8080+k", () => {
    expect(forwardRows(5)).toEqual([1, 2, 3, 4, 5].map((k) => ({ camera: k, name: null, rtsp: 5540 + k, onvif: 8080 + k })));
    expect(forwardRows(2, 6000, 9000, ["Gate"])).toEqual([{ camera: 1, name: "Gate", rtsp: 6001, onvif: 9001 }, { camera: 2, name: null, rtsp: 6002, onvif: 9002 }]);
  });
  it("at least five rows, more when the instance has more cameras", () => {
    expect(forwardCount(0)).toBe(5);
    expect(forwardCount(8)).toBe(8);
  });
});

describe("VPN sheet and form", () => {
  it("subnet and gateway", () => {
    expect(defaultSubnet(7)).toBe("10.20.7.0/24");
    expect(lanGateway("10.20.7.0/24")).toBe("10.20.7.1");
    expect(lanGateway("nonsense")).toBeNull();
  });
  it("next free site number", () => {
    expect(nextSiteNumber([])).toBe(1);
    expect(nextSiteNumber([1, 2, null, 4])).toBe(3);
    expect(nextSiteNumber(Array.from({ length: 250 }, (_, i) => i + 1))).toBeNull();
  });
  it("form checks", () => {
    expect(centralFormError({ mode: "vpn", public_ip: "", subnet: "10.20.7.0/24", quota_gb: "2000" })).toBeNull();
    expect(centralFormError({ mode: "vpn", public_ip: "", subnet: "", quota_gb: "2000" })).toBeNull();
    expect(centralFormError({ mode: "vpn", public_ip: "", subnet: "8.8.8.0/24", quota_gb: "2000" })).toMatch(/private/);
    expect(centralFormError({ mode: "forward", public_ip: "", subnet: "", quota_gb: "2000" })).toMatch(/public IP/);
    expect(centralFormError({ mode: "forward", public_ip: "93.184.216.34", subnet: "", quota_gb: "0" })).toMatch(/quota/);
    expect(centralFormError({ mode: "vpn", public_ip: "", subnet: "", quota_gb: "9" })).toMatch(/at least 10/);   // the host agent's minimum
  });
});

describe("phases and capacity", () => {
  it("progress steps", () => {
    expect(phaseStep("provisioning")).toBe(0);
    expect(phaseStep("waiting_enroll")).toBe(1);
    expect(phaseStep("running")).toBe(2);
    expect(phaseStep("failed")).toBe(-1);
    expect(settling({ phase: "waiting_enroll" })).toBe(true);
    expect(settling({ phase: "running" })).toBe(false);
  });
  it("bars for CPU, RAM, each GPU and disk", () => {
    const bars = capacityBars({ cpus: 80, load: 20, ram_gb: { total: 768, free: 600 }, gpus: [{ index: 1, name: "NVIDIA A10", mem_total_mb: 23028, mem_used_mb: 11514, util: 40 }],
      disks: [{ path: "/srv/axiom", total_gb: 10000, free_gb: 2500 }] });
    expect(bars.map((b) => [b.label, b.pct])).toEqual([["CPU", 25], ["RAM", 22], ["GPU 1 · A10", 50], ["Disk /srv/axiom", 75]]);
    expect(capacityBars(null)).toEqual([]);
  });
});

describe("camera addresses", () => {
  it("accepts LAN / VPN subnets, public IPs and DNS names", () => {
    for (const v of ["192.168.105.0/24", "10.20.7.0/24", "172.16.0.0/16", "100.64.12.0/24", "192.168.105.7/24", "10.30.1.5/32", ""])
      expect(cameraEntryError("subnets", v), v).toBeNull();
    for (const v of ["203.0.113.7", "93.184.216.34", "192.168.1.20"]) expect(cameraEntryError("public_ips", v), v).toBeNull();
    for (const v of ["cam1.example.net", "Cam-2.Dyn.Example.ORG.", "x.y"]) expect(cameraEntryError("hosts", v), v).toBeNull();
  });
  it("refuses what the hub refuses", () => {
    for (const v of ["0.0.0.0/0", "10.0.0.0/8", "192.168.0.0/15", "8.8.8.0/24", "10.200.3.0/24", "10.200.0.0/16", "10.201.0.0/24",
      "10.201.0.128/25", "127.0.0.0/16", "192.168.105.0", "192.168.105.0/33", "999.1.1.0/24", "nonsense"])
      expect(cameraEntryError("subnets", v), v).not.toBeNull();
    expect(cameraEntryError("subnets", "10.0.0.0/8")).toBe("Broader than a /16");
    expect(cameraEntryError("subnets", "10.200.3.0/24")).toContain("instance pool");
    for (const v of ["127.0.0.1", "0.0.0.0", "169.254.1.1", "224.0.0.1", "255.255.255.255", "240.0.0.1", "10.200.0.2", "10.201.0.10", "1.2.3", "cam.example.net"])
      expect(cameraEntryError("public_ips", v), v).not.toBeNull();
    expect(cameraEntryError("public_ips", "cam.example.net")).toBe("A DNS name: put it under host names");
    for (const v of ["localhost", "cam", "-cam.example.net", "cam-.example.net", "cam..example.net", "cam_1.example.net", "1.2.3.4", "999.1.1.1",
      "cam.localhost", "cam example.net", `${"a".repeat(64)}.example.net`])
      expect(cameraEntryError("hosts", v), v).not.toBeNull();
    expect(cameraEntryError("hosts", "1.2.3.4")).toBe("An IP address: put it under public IPs");
    expect(isHostName("x.".repeat(130) + "net")).toBe(false);
    expect(parseIPv4("192.168.1.256")).toBeNull();
    expect(parseIPv4("255.255.255.255")).toBe(0xffffffff);
  });
  it("cleans, checks the whole list and caps it at 32", () => {
    const l = { subnets: [" 192.168.105.0/24 ", "", "192.168.105.0/24"], public_ips: ["203.0.113.7"], hosts: ["Cam1.Example.net.", "cam1.example.net"] };
    expect(cleanCameraNetwork(l)).toEqual({ subnets: ["192.168.105.0/24"], public_ips: ["203.0.113.7"], hosts: ["cam1.example.net"] });
    expect(cameraNetworkError(l)).toBeNull();
    expect(cameraNetworkError({ ...l, public_ips: ["cam.example.net"] })).toBe("cam.example.net: A DNS name: put it under host names");
    const many = { subnets: Array.from({ length: 30 }, (_, i) => `10.30.${i}.0/24`), public_ips: ["203.0.113.7", "203.0.113.8"], hosts: ["cam1.example.net"] };
    expect(cameraNetworkError(many)).toBe("33 addresses: at most 32");
    expect(cameraNetworkError({ ...many, hosts: [] })).toBeNull();
  });
  it("summary line, resolved names and the routers the port-forward table applies to", () => {
    const l = { subnets: ["192.168.105.0/24"], public_ips: ["203.0.113.7"], hosts: ["cam1.example.net"] };
    expect(cameraNetworkSummary(l)).toBe("Cameras reachable at: 192.168.105.0/24 · 203.0.113.7 · cam1.example.net");
    expect(cameraNetworkSummary({ subnets: [], public_ips: [], hosts: [] })).toBe("Cameras reachable at: none");
    expect(resolvedLines(["cam1.example.net", "cam2.example.net"], { "cam1.example.net": ["203.0.113.21"] }))
      .toEqual(["cam1.example.net → 203.0.113.21", "cam2.example.net → not resolved yet"]);
    expect(forwardTargets({ forward_addresses: ["203.0.113.7", "cam1.example.net"], public_ip: null })).toEqual(["203.0.113.7", "cam1.example.net"]);
    expect(forwardTargets({ public_ip: "93.184.216.34" })).toEqual(["93.184.216.34"]);   // an older hub's answer
    expect(forwardTargets({ forward_addresses: [], public_ip: "93.184.216.34" })).toEqual([]);
  });
  it("reads the instance's camera network, or derives it from an older answer", () => {
    expect(cameraNetworkOf({ mode: "vpn", subnet: "10.20.7.0/24", public_ip: null, camera_network: { subnets: ["192.168.105.0/24"], public_ips: [], hosts: ["a.example.net"] } }))
      .toEqual({ subnets: ["192.168.105.0/24"], public_ips: [], hosts: ["a.example.net"] });
    expect(cameraNetworkOf({ mode: "vpn", subnet: "10.20.7.0/24", public_ip: null })).toEqual({ subnets: ["10.20.7.0/24"], public_ips: [], hosts: [] });
    expect(cameraNetworkOf({ mode: "forward", subnet: null, public_ip: "93.184.216.34" })).toEqual({ subnets: [], public_ips: ["93.184.216.34"], hosts: [] });
    expect(cameraNetworkOf({ mode: "forward", subnet: null, public_ip: "yard.dyn.example.net" })).toEqual({ subnets: [], public_ips: [], hosts: ["yard.dyn.example.net"] });
  });
});

describe("host capacity from a real agent", () => {
  it("reads the load averages list the host agent sends (a number crashed the Hosts page)", () => {
    const bars = capacityBars({ cpus: 80, load: [0.31, 0.21, 0.4], ram_gb: { total: 791.2, free: 707.5 } });
    expect(bars[0].text).toBe("load 0.3 on 80 cores");
    expect(capacityBars({ cpus: 8, load: 2 })[0].used).toBe(2);
  });
});

describe("camera limit", () => {
  it("cameras against the limit", () => {
    expect(camerasText(3, 5)).toBe("3 of 5");
    expect(camerasText(0, null)).toBe("0 (no limit)");
    expect(camerasText(undefined, 2)).toBe("0 of 2");
    expect(overLimit(4, 3)).toBe(true);
    expect(overLimit(3, 3)).toBe(false);
    expect(overLimit(9, null)).toBe(false);
  });
  it("the limit field: blank = none, 1-500", () => {
    expect(parseCameraLimit("")).toEqual({ value: null, error: null });
    expect(parseCameraLimit(" 12 ")).toEqual({ value: 12, error: null });
    for (const bad of ["0", "501", "2.5", "-1", "ten"]) expect(parseCameraLimit(bad).error).toMatch(/1 to 500/);
  });
});

describe("allocate form", () => {
  const site = (id: string, name: string) => ({ id, name } as Site);
  const orgs = [{ org: { id: "o1", name: "Acme" }, locations: [site("l1", "Yard"), site("l2", "Annex"), site("l3", "Depot")], unassigned: [] }] as HubSitesOrg[];
  it("offers the customer's Sites without a live instance", () => {
    const inst = [{ location_id: "l1", state: "running" as const }, { location_id: "l3", state: "deleted" as const }];
    expect(sitesWithoutCentral(orgs, "o1", inst)).toEqual([{ id: "l2", name: "Annex" }, { id: "l3", name: "Depot" }]);
    expect(sitesWithoutCentral(orgs, "nope", [])).toEqual([]);
  });
  const ok = { org: "o1", site: "l2", mode: "vpn" as const, subnet: "10.20.7.0/24", public_ip: "", quota_gb: "2000", camera_limit: "5" };
  it("enables Allocate only when the hub would accept it", () => {
    expect(allocateFormError(ok, 8000)).toBeNull();
    expect(allocateFormError({ ...ok, camera_limit: "" }, null)).toBeNull();
    expect(allocateFormError({ ...ok, org: "" }, 8000)).toMatch(/customer/);
    expect(allocateFormError({ ...ok, site: "" }, 8000)).toMatch(/Site/);
    expect(allocateFormError({ ...ok, camera_limit: "0" }, 8000)).toMatch(/camera limit/);
    expect(allocateFormError({ ...ok, quota_gb: "9000" }, 8000)).toBe("The host has room for 8.0 TB");
    expect(allocateFormError({ ...ok, mode: "forward", public_ip: "" }, 8000)).toMatch(/public IP/);
    expect(allocateFormError({ ...ok, mode: "forward", public_ip: "not an address" }, 8000)).toMatch(/IP address or a DNS name/);
    expect(allocateFormError({ ...ok, mode: "forward", public_ip: "yard.dyn.example.net" }, 8000)).toBeNull();
  });
  it("splits the firewall into Site networks and the cameras' own addresses", () => {
    const ci = { mode: "vpn" as const, subnet: "10.20.7.0/24", public_ip: null,
      camera_network: { subnets: ["10.20.7.0/24"], public_ips: ["203.0.113.7"], hosts: [], auto: { public_ips: ["203.0.113.7", "93.184.216.77"], hosts: ["yard.dyn.example.net"] } } };
    expect(addressParts(ci)).toEqual({ site: ["10.20.7.0/24", "203.0.113.7"], cameras: ["93.184.216.77", "yard.dyn.example.net"] });
    expect(addressParts({ mode: "vpn", subnet: "10.20.8.0/24", public_ip: null })).toEqual({ site: ["10.20.8.0/24"], cameras: [] });
  });
});

describe("central instance on its server card (Site › Servers)", () => {
  const inst = [{ id: "c1", server_id: "s_central" }, { id: "c2", server_id: null }, { id: "c3", server_id: "s_elsewhere" }];
  it("matches the instance to its server's card; the rest get placeholder cards", () => {
    expect(instanceFor(inst, "s_central")?.id).toBe("c1");
    expect(instanceFor(inst, "s_box")).toBeUndefined();
    expect(instanceFor(null, "s_central")).toBeUndefined();
    expect(unenrolledInstances(inst, [{ id: "s_central" }, { id: "s_box" }]).map((c) => c.id)).toEqual(["c2", "c3"]);
    expect(unenrolledInstances(inst, [{ id: "s_central" }, { id: "s_elsewhere" }]).map((c) => c.id)).toEqual(["c2"]);
    expect(unenrolledInstances(undefined, [])).toEqual([]);
  });
  it("datacenter line: host and GPU for hub administrators only", () => {
    const ci = { host_id: "h_1", host_name: "fred-001", host_online: true, gpu: 0, gpu_name: "NVIDIA A40" };
    expect(datacenterLine(ci, true)).toBe("Datacenter · fred-001 · GPU 0 A40");
    expect(datacenterLine({ ...ci, host_online: false, gpu: null }, true)).toBe("Datacenter · fred-001 (offline) · no GPU");
    expect(datacenterLine({ ...ci, host_name: null }, true)).toBe("Datacenter · h_1 · GPU 0 A40");
    expect(datacenterLine(ci, false)).toBe(CENTRAL_TAG);
    // a customer's answer has no host fields at all
    expect(datacenterLine({}, true)).toBe("Datacenter (central recording)");
  });
  it("connection, limit, addresses and the customer hint", () => {
    expect(connectionText({ mode: "vpn", subnet: "10.20.7.0/24" })).toBe("VPN · 10.20.7.0/24");
    expect(connectionText({ mode: "vpn", subnet: null })).toBe("VPN · —");
    expect(connectionText({ mode: "forward", subnet: null })).toBe("Port forwarding");
    expect(limitText(3, 5)).toBe("3 of 5 cameras");
    expect(limitText(undefined, 1)).toBe("0 of 1 camera");
    expect(limitText(6, 5)).toBe("6 of 5 cameras · over");
    expect(limitText(4, null)).toBe("no limit");
    expect(reachableText({ mode: "vpn", subnet: "10.20.7.0/24", public_ip: null,
      camera_network: { subnets: ["10.20.7.0/24"], public_ips: [], hosts: [], auto: { public_ips: [], hosts: ["yard.dyn.example.net"] } } }))
      .toBe("Cameras reachable at: 10.20.7.0/24 · opened for its cameras: yard.dyn.example.net");
    expect(reachableText({ mode: "forward", subnet: null, public_ip: null, camera_network: { subnets: [], public_ips: [], hosts: [] } })).toBe("Cameras reachable at: no Site network");
    expect(addCamerasHint(5)).toMatch(/console.*Up to 5 cameras; ask Axiom Vision for more\.$/);
    expect(addCamerasHint(1)).toMatch(/Up to 1 camera;/);
    expect(addCamerasHint(null)).not.toMatch(/Up to/);
  });
});
