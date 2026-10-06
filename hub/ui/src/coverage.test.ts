import { describe, expect, it } from "vitest";
import { canCheckCoverage } from "./access";
import type { CoverageCarrier, CoverageData, CoverageEntry, Me, Org } from "./api";
import { bestCarrier, chipText, needFor, needText, radiusText, ringClass, ringsFor, signalBand, sortCarriers, testsText, uploadFit } from "./coverage";

const entry = (technology: string, overall: number | null, coverage = overall, r = { r05: 1, r1: 0.95, r2: 0.7 }): CoverageEntry => ({
  technology, technology_name: technology,
  summary: overall == null ? null : { overall, performance: overall, coverage, reliability: overall, is_fully_covered: true, source: "measured", accuracy: "exact" },
  fcc: { signal: { point: -80, r05: -81, r1: -82, r2: -84 }, coverage: r }, speed: null,
});
const carrier = (code: string, lte: number | null, g5: number | null, cov?: number): CoverageCarrier =>
  ({ code, name: code, best: null, best_technology: null, tech: { lte: entry("lte", lte, cov ?? lte), "5g": entry("5g", g5, cov ?? g5) } });

describe("signalBand (CoverageMap's dBm bands)", () => {
  it("excellent, good, fair, poor at the documented edges", () => {
    expect(signalBand(-55)).toBe("excellent");
    expect(signalBand(-70)).toBe("excellent");
    expect(signalBand(-70.1)).toBe("good");
    expect(signalBand(-85)).toBe("good");
    expect(signalBand(-85.1)).toBe("fair");
    expect(signalBand(-100)).toBe("fair");
    expect(signalBand(-100.5)).toBe("poor");
    expect(signalBand(-120)).toBe("poor");
  });
  it("no modeled signal", () => {
    expect(signalBand(null)).toBe("none");
    expect(signalBand(undefined)).toBe("none");
  });
});

describe("uploadFit (mirrors hub coverage.upload_fit)", () => {
  const up = (med: number | null, count = 10, failed = 0, min: number | null = null) => ({ med, count, failed, min });
  it("fits at 1.5x the need, tight at the need, won't fit below", () => {
    expect(uploadFit(20, up(30)).fit).toBe("fits");
    expect(uploadFit(20, up(29.9)).fit).toBe("tight");
    expect(uploadFit(20, up(20)).fit).toBe("tight");
    expect(uploadFit(20, up(19.9)).fit).toBe("wont_fit");
    expect(uploadFit(20, up(30)).ratio).toBe(1.5);
  });
  it("unknown without tests or without a need; failed tests only = won't fit", () => {
    expect(uploadFit(20, null).fit).toBe("unknown");
    expect(uploadFit(20, up(null, 0, 0)).fit).toBe("unknown");
    expect(uploadFit(0, up(40)).fit).toBe("unknown");
    expect(uploadFit(20, up(null, 0, 3)).fit).toBe("wont_fit");
  });
  it("many failed tests turn a fit into tight; a slow test is mentioned", () => {
    expect(uploadFit(20, up(40, 6, 2)).fit).toBe("tight");
    expect(uploadFit(20, up(40, 9, 2)).fit).toBe("fits");
    expect(uploadFit(20, up(40, 10, 0, 5)).reason).toContain("slowest test 5");
  });
  it("the address check's need: cameras at 6 Mbit/s", () => {
    expect(needFor(5).mbps).toBe(30);
    expect(needFor(-2).cameras).toBe(0);
    expect(needText(needFor(5))).toBe("5 cameras · 30 Mbit/s (assumed: 6 Mbit/s per main stream, 1 per sub stream)");
    expect(needText({ mbps: 16.5, cameras: 4, measured: 2, assumed: 2, typical: false })).toBe("4 cameras · 16.5 Mbit/s (2 measured, 2 assumed)");
    expect(needText({ mbps: 30, cameras: 5, measured: 0, assumed: 5, typical: true })).toContain("typical site");
  });
});

describe("carriers", () => {
  it("orders by the best overall score of any technology, then coverage, then code", () => {
    const list = [carrier("ATT", 6.2, 0), carrier("VZW", 8.6, 7.1), carrier("TMO", 7.5, 9.0)];
    expect(sortCarriers(list).map((c) => c.code)).toEqual(["TMO", "VZW", "ATT"]);
    const tie = [carrier("VZW", 8, 8, 7), carrier("ATT", 8, 8, 9), carrier("TMO", 8, 8, 9)];
    expect(sortCarriers(tie).map((c) => c.code)).toEqual(["ATT", "TMO", "VZW"]);
    const unscored = [carrier("ATT", null, null), carrier("VZW", 2, null)];
    expect(sortCarriers(unscored).map((c) => c.code)).toEqual(["VZW", "ATT"]);
  });
  it("best carrier and the header chip", () => {
    const d: CoverageData = { latitude: 1, longitude: 2, carriers: [carrier("ATT", 6.2, 0), carrier("VZW", 8.6, 7.1)] };
    expect(bestCarrier(d)).toEqual({ code: "VZW", name: "Verizon", technology: "lte", score: 8.6 });
    expect(chipText(d)).toBe("📶 VZW 8.6");
    expect(chipText({ latitude: 1, longitude: 2, carriers: [carrier("ATT", null, null)] })).toBeNull();
    expect(chipText(null)).toBeNull();
  });
});

describe("rings and texts", () => {
  it("ring colors by covered share", () => {
    expect(ringClass(1)).toBe("good");
    expect(ringClass(0.9)).toBe("good");
    expect(ringClass(0.89)).toBe("fair");
    expect(ringClass(0.6)).toBe("fair");
    expect(ringClass(0.2)).toBe("poor");
    expect(ringClass(0)).toBe("none");
    expect(ringClass(null)).toBe("none");
  });
  it("rings largest first, labelled as averages", () => {
    const r = ringsFor(entry("lte", 8), "VZW LTE");
    expect(r.map((x) => [x.radius_m, x.cls])).toEqual([[2000, "fair"], [1000, "good"], [500, "good"]]);
    expect(r[0].label).toContain("70% of the area within 2 km");
    expect(r[0].label).toContain("average");
    expect(ringsFor(null, "x").every((x) => x.cls === "none")).toBe(true);
  });
  it("where the tests are and how many", () => {
    const m = { radius: "1km" as const, med: 15, min: 4, avg: 16, max: 30, count: 9, failed: 1, accuracy: "high" };
    expect(radiusText(m)).toBe("within 1 km");
    expect(radiusText({ ...m, radius: "closest", distance_km: 4.2 })).toBe("closest tests 4.2 km away");
    expect(testsText(m)).toBe("9 tests · 1 failed");
    expect(testsText(null)).toBe("no tests");
  });
});

describe("canCheckCoverage (who may spend units)", () => {
  const me = (plan: "trial" | "paid" | null, is_super = false, visible = true) =>
    ({ user: { id: "u", email: "e", totp_enabled: false, is_super }, orgs: [], active_org: null,
       coverage: { enabled: plan !== null, plan, visible, evaluation: plan === "trial", cost_per_lookup: 4 } }) as Me;
  const org = (role: string, extra: Partial<Org> = {}): Org => ({ id: "o", name: "O", slug: "o", role, ...extra });
  it("trial: hub administrators only", () => {
    expect(canCheckCoverage(me("trial", true), org("owner"))).toBe(true);
    expect(canCheckCoverage(me("trial", false, false), org("admin"))).toBe(false);
  });
  it("paid: customer admins with a real membership", () => {
    expect(canCheckCoverage(me("paid"), org("admin"))).toBe(true);
    expect(canCheckCoverage(me("paid"), org("owner", { member: true }))).toBe(true);
    expect(canCheckCoverage(me("paid"), org("operator"))).toBe(false);
    expect(canCheckCoverage(me("paid"), org("admin", { soc: true }))).toBe(false);
  });
  it("off: nobody", () => {
    expect(canCheckCoverage(me(null, true, false), org("owner"))).toBe(false);
  });
});
