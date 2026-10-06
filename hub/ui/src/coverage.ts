/**
 * Cellular coverage (hub/hub/coverage.py, the CoverageMap API), the pure parts: signal bands, the upload fit for the
 * cameras, best-carrier ordering, the map's ring colors and the short texts the card, the header chip and the address
 * check show. Kept apart from the components so they are unit-tested (coverage.test.ts). The upload fit mirrors the
 * hub's coverage.upload_fit (same thresholds).
 */
import type { CameraNeed, CoverageCarrier, CoverageData, CoverageEntry, CoverageMetric, FitKind, UploadFit } from "./api";

export const TECHS = ["lte", "5g"] as const;
export const TECH_LABEL: Record<string, string> = { lte: "LTE", "5g": "5G", all: "All" };
export const CARRIER_SHORT: Record<string, string> = { ATT: "AT&T", VZW: "Verizon", TMO: "T-Mobile" };
export const carrierName = (c: Pick<CoverageCarrier, "code" | "name">) => CARRIER_SHORT[c.code] ?? c.name ?? c.code;

// ---------------------------------------------------------------- signal (the vendor's bands)

export type SignalBand = "excellent" | "good" | "fair" | "poor" | "none";
/** -50..-70 dBm excellent, -70..-85 good, -85..-100 fair, below -100 poor; null = no modeled signal. */
export function signalBand(dbm: number | null | undefined): SignalBand {
  if (dbm == null || !isFinite(dbm)) return "none";
  if (dbm >= -70) return "excellent";
  if (dbm >= -85) return "good";
  if (dbm >= -100) return "fair";
  return "poor";
}
export const BAND_LABEL: Record<SignalBand, string> = { excellent: "Excellent", good: "Good", fair: "Fair", poor: "Poor", none: "No signal" };

// ---------------------------------------------------------------- upload fit

const HEADROOM = 1.5, FAILED_SHARE_TIGHT = 0.25;
export const ASSUMED_MAIN_MBPS = 6;
export const TYPICAL_CAMERAS = 5;

/**
 * Will the measured upload carry the cameras? fits: median ≥ 1.5× the need; tight: ≥ the need; wont_fit: below it, or
 * every nearby test failed; unknown: no tests (or nothing needed). A "fits" with a quarter of the tests failed is tight.
 */
export function uploadFit(need: number, up: Pick<CoverageMetric, "med" | "count" | "failed" | "min"> | null | undefined): UploadFit {
  if (!(need > 0) || !up) return { fit: "unknown", ratio: null, reason: need > 0 ? "no speed tests nearby" : "no cameras" };
  const count = up.count ?? 0, failed = up.failed ?? 0, med = up.med;
  if (count === 0 || med == null) {
    return failed > 0 ? { fit: "wont_fit", ratio: null, reason: `all ${failed} nearby upload test${failed === 1 ? "" : "s"} failed` }
      : { fit: "unknown", ratio: null, reason: "no speed tests nearby" };
  }
  let fit: FitKind = med >= HEADROOM * need ? "fits" : med >= need ? "tight" : "wont_fit";
  let reason = `median upload ${fmtNum(med)} Mbit/s for ${fmtNum(need)} Mbit/s needed`;
  if (fit === "fits" && failed / (count + failed) >= FAILED_SHARE_TIGHT) { fit = "tight"; reason += `; ${failed} of ${count + failed} tests failed`; }
  if (fit === "fits" && up.min != null && up.min < need) reason += `; slowest test ${fmtNum(up.min)} Mbit/s`;
  return { fit, ratio: Math.round((med / need) * 100) / 100, reason };
}
export const FIT_LABEL: Record<FitKind, string> = { fits: "Fits", tight: "Tight", wont_fit: "Won't fit", unknown: "No tests" };

/** The address check's need: `cameras` on main streams at the assumed rate. */
export const needFor = (cameras: number): CameraNeed => {
  const n = Math.max(0, Math.min(64, Math.floor(cameras || 0)));
  return { mbps: n * ASSUMED_MAIN_MBPS, cameras: n, measured: 0, assumed: n, typical: false };
};

/** "5 cameras · 30 Mbit/s (3 measured, 2 assumed at 6 Mbit/s)" */
export function needText(n: CameraNeed): string {
  const head = `${n.cameras} camera${n.cameras === 1 ? "" : "s"} · ${fmtNum(n.mbps)} Mbit/s`;
  if (n.typical) return `${head} (a typical site: ${TYPICAL_CAMERAS} cameras at ${ASSUMED_MAIN_MBPS} Mbit/s; no cameras reported yet)`;
  if (!n.assumed) return `${head} (measured)`;
  if (!n.measured) return `${head} (assumed: ${ASSUMED_MAIN_MBPS} Mbit/s per main stream, 1 per sub stream)`;
  return `${head} (${n.measured} measured, ${n.assumed} assumed)`;
}

// ---------------------------------------------------------------- carriers

const score = (e: CoverageEntry | undefined, f: "overall" | "coverage" = "overall") => e?.summary?.[f] ?? -1;
/** A carrier's best overall score over its technologies (null when it has no scores). */
export function carrierBest(c: Pick<CoverageCarrier, "tech">): number | null {
  const s = Math.max(-1, ...Object.values(c.tech).map((e) => score(e)));
  return s >= 0 ? s : null;
}
/** Best first: highest overall score of any technology, then coverage score, then code (the hub sends them sorted too). */
export function sortCarriers<T extends Pick<CoverageCarrier, "code" | "tech">>(carriers: T[]): T[] {
  const cov = (c: T) => Math.max(-1, ...Object.values(c.tech).map((e) => score(e, "coverage")));
  return [...carriers].sort((a, b) => (carrierBest(b) ?? -1) - (carrierBest(a) ?? -1) || cov(b) - cov(a) || a.code.localeCompare(b.code));
}
/** The best carrier and technology at the place, or null when nothing scored. */
export function bestCarrier(d: CoverageData | null | undefined): { code: string; name: string; technology: string; score: number } | null {
  const c = sortCarriers(d?.carriers ?? [])[0];
  const best = c ? carrierBest(c) : null;
  if (!c || best == null) return null;
  const technology = Object.entries(c.tech).sort((a, b) => score(b[1]) - score(a[1]))[0]?.[0] ?? "";
  return { code: c.code, name: carrierName(c), technology, score: best };
}
/** The Site header chip: "📶 VZW 8.6" (null: nothing to show). */
export function chipText(d: CoverageData | null | undefined): string | null {
  const b = bestCarrier(d);
  return b ? `📶 ${b.code} ${fmtScore(b.score)}` : null;
}

// ---------------------------------------------------------------- scores and numbers

export const fmtNum = (v: number) => (Math.abs(v) >= 100 ? String(Math.round(v)) : String(Math.round(v * 10) / 10));
export const fmtScore = (v: number | null | undefined) => (v == null || !isFinite(v) ? "—" : v.toFixed(1));
/** 7+ good, 4+ fair, else poor (bar colors). */
export const scoreClass = (v: number | null | undefined) => (v == null ? "none" : v >= 7 ? "good" : v >= 4 ? "fair" : "poor");
export const pctText = (f: number | null | undefined) => (f == null || !isFinite(f) ? "—" : `${Math.round(f * 100)}%`);
/** "18.6 Mbit/s" / "24 ms" (median), "—" without tests. */
export const medText = (m: CoverageMetric | null | undefined, unit: string) => (m?.med == null ? "—" : `${fmtNum(m.med)} ${unit}`);
/** Where the tests behind a metric are: "within 0.5 km", "closest tests 4.2 km away". */
export function radiusText(m: CoverageMetric | null | undefined): string {
  if (!m) return "";
  if (m.radius === "closest") return m.distance_km != null ? `closest tests ${fmtNum(m.distance_km)} km away` : "closest tests";
  return `within ${m.radius.replace("km", " km")}`;
}
/** "12 tests · 1 failed" */
export function testsText(m: CoverageMetric | null | undefined): string {
  if (!m) return "no tests";
  return `${m.count} test${m.count === 1 ? "" : "s"}${m.failed ? ` · ${m.failed} failed` : ""}`;
}
export const unitsText = (n: number) => `${n} unit${n === 1 ? "" : "s"}`;
/** The confirmation line before a lookup: speed tests only bill when found. */
export const costText = (n: number) => `up to ${unitsText(n)} (speed tests bill 2 units only where tests exist)`;

// ---------------------------------------------------------------- the map's rings

export type RingClass = "good" | "fair" | "poor" | "none";
/** Covered share of a radius → ring color: 90%+ good, 60%+ fair, some poor, none (or unknown) grey. */
export function ringClass(fraction: number | null | undefined): RingClass {
  if (fraction == null || !isFinite(fraction) || fraction <= 0) return "none";
  if (fraction >= 0.9) return "good";
  if (fraction >= 0.6) return "fair";
  return "poor";
}
export type Ring = { radius_m: number; fraction: number | null; cls: RingClass; label: string };
/** Rings for one carrier/technology, largest first (drawn under the smaller ones). */
export function ringsFor(e: CoverageEntry | null | undefined, title: string): Ring[] {
  const cov = e?.fcc?.coverage;
  return ([[2000, cov?.r2, "2 km"], [1000, cov?.r1, "1 km"], [500, cov?.r05, "0.5 km"]] as const).map(([r, f, name]) => ({
    radius_m: r, fraction: f ?? null, cls: ringClass(f),
    label: `${title}: ${f == null ? "no FCC data" : `${pctText(f)} of the area`} within ${name} (an average for the whole circle, not a map of where)`,
  }));
}
