import { describe, expect, it } from "vitest";
import {
  capacityBars, centralFormError, defaultSubnet, fmtGB, fmtMbps, forwardCount, forwardRows, lanGateway, nextSiteNumber, pct, phaseStep,
  quotaText, settling, siteBandwidth, uploadLine,
} from "./central";
import type { ServerSummary } from "./api";

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
