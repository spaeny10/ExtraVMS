/**
 * License plates (backend plate.py) and the Ask answer's references, as pure helpers:
 *  - formatPlate: "ABC1234", "TX" -> "ABC 1234 · TX" (letters and digits apart, like the backend's plate.display);
 *  - plateOutcome: what "Read plate" says after a run ("ABC 1234 · TX", "Not readable in this footage", ...);
 *  - plateError: a failed run's message (rate limit, role, Qwen down);
 *  - splitCited: the references of an answer, the cited ones first (in citation order), the rest folded under
 *    "Other events searched (N)".
 */
import type { PlateRead } from "./api";

export function formatPlate(plate: string | null | undefined, region?: string | null): string {
  if (!plate) return "";
  const text = plate.toUpperCase().replace(/[^A-Z0-9]/g, "").replace(/([A-Z])(?=\d)|(\d)(?=[A-Z])/g, "$1$2 ");
  return region ? `${text} · ${region}` : text;
}

/** The line "Read plate" shows when it is done: the plate, or why there is none. */
export function plateOutcome(r: Pick<PlateRead, "result" | "plate" | "region" | "reason">): string {
  if (r.result === "read" && r.plate) return `Plate ${formatPlate(r.plate, r.region)}`;
  if (r.result === "skipped" && r.reason?.startsWith("too small")) return "Not readable in this footage (the vehicle is too small)";
  if (r.result === "skipped" && r.reason === "no footage") return "Not readable: the footage is no longer there";
  return "Not readable in this footage";
}

/** "429 {"detail":"this plate was just read; try again in 25 s"}" -> the detail; a viewer's 403 says who may. */
export function plateError(e: unknown): string {
  const s = String(e instanceof Error ? e.message : e);
  const m = /^(\d{3})\s+(.*)$/s.exec(s);
  let detail = m ? m[2] : s;
  try { const j = JSON.parse(detail); if (j && typeof j.detail === "string") detail = j.detail; } catch { /* plain text */ }
  if (m?.[1] === "403") return "Reading a plate needs the operator role";
  return detail.slice(0, 200) || "The plate could not be read";
}

const CITE = /\[#(\d+[a-z]?)\]/g;

/** The event refs an answer cites, in order, once each ("[#12] ... [#7] ... [#12]" -> ["12", "7"]). */
export function citedRefs(text: string): string[] {
  return [...new Set([...text.matchAll(CITE)].map((m) => m[1]))];
}

/**
 * An answer's references: the ones it cites first, in the order it cites them, then the others (as given: the
 * caller orders them, e.g. newest first). Citations of refs that aren't in the list are ignored.
 */
export function splitCited<T>(items: readonly T[], refOf: (x: T) => string | null | undefined, text: string): { cited: T[]; other: T[] } {
  const order = new Map(citedRefs(text).map((r, i) => [r, i]));
  const cited = items.filter((x) => { const r = refOf(x); return r != null && order.has(r); })
    .sort((a, b) => order.get(refOf(a)!)! - order.get(refOf(b)!)!);
  const other = items.filter((x) => { const r = refOf(x); return r == null || !order.has(r); });
  return { cited, other };
}

/** "Other events searched (3)" */
export const otherLabel = (n: number) => `Other events searched (${n})`;
