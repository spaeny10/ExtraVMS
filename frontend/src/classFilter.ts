/**
 * The "Show [People] [Vehicles] [PPE violations]" pills: the Timeline's filter bar and Live → "Latest activity" (LiveView
 * here, Site Live on the hub through @site/liveFeed) decide with the same predicate.
 */

export type ClassFilter = {
  person: boolean;
  vehicle: boolean;
  /** only events that broke a PPE rule (the Timeline's hatched markers) */
  ppe: boolean;
};

export const DEFAULT_CLASS_FILTER: ClassFilter = { person: true, vehicle: true, ppe: false };

/** What says an event broke a PPE rule: the Timeline's `ppe` column (backend: json_extract(policy, '$.kind') = 'ppe'),
 *  or an event list's broken rule itself (`policy.kind`), which is what that column is computed from. */
export type PpeMarked = { ppe?: number | boolean | null; policy?: { kind: string } | null };

export const isPpeViolation = (e: PpeMarked): boolean => !!e.ppe || e.policy?.kind === "ppe";

/** People and vehicles by their pill (any other label always passes them); with PPE on, only PPE violations. */
export function classPass(e: { camera_class: string } & PpeMarked, f: ClassFilter): boolean {
  if (e.camera_class === "person" ? !f.person : e.camera_class === "vehicle" ? !f.vehicle : false) return false;
  if (f.ppe && !isPpeViolation(e)) return false;
  return true;
}

/** A stored choice (localStorage JSON) over the defaults: bad JSON or a missing / non-boolean field keeps the default. */
export function parseClassFilter(raw: string | null | undefined): ClassFilter {
  let v: unknown;
  try {
    v = JSON.parse(raw ?? "null");
  } catch {
    return { ...DEFAULT_CLASS_FILTER };
  }
  const o = v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
  const pick = (k: keyof ClassFilter) => (typeof o[k] === "boolean" ? (o[k] as boolean) : DEFAULT_CLASS_FILTER[k]);
  return { person: pick("person"), vehicle: pick("vehicle"), ppe: pick("ppe") };
}

/**
 * The pills as filters for the feed's fetches, so "Latest activity" lists the latest MATCHING events rather than the
 * few matches among the latest events of every kind (2026-10-20: People off showed 3 vehicle cards). One class on
 * narrows to that class; PPE on asks for PPE violations only. null: nothing to narrow (the defaults, or both classes
 * off, which only lets other labels through: the unfiltered recent list carries those). The cards still go through
 * classPass, so a server or hub that ignores a param shows nothing it shouldn't.
 */
export type FeedNarrow = { label: "person" | "vehicle" | null; ppe: boolean };

export function feedNarrow(f: ClassFilter): FeedNarrow | null {
  const label = f.person && !f.vehicle ? "person" : f.vehicle && !f.person ? "vehicle" : null;
  return label || f.ppe ? { label, ppe: f.ppe } : null;
}

/** For a server's GET /api/events (EventQuery): label= and flags=ppe. */
export function nvrFeedQuery(n: FeedNarrow | null): { label?: string; flags?: string[] } {
  if (!n) return {};
  return { ...(n.label ? { label: n.label } : {}), ...(n.ppe ? { flags: ["ppe"] } : {}) };
}

/** For the hub's GET /api/locations/{id}/events: classes= and ppe=true. */
export function hubFeedQuery(n: FeedNarrow | null): { classes?: string[]; ppe?: boolean } {
  if (!n) return {};
  return { ...(n.label ? { classes: [n.label] } : {}), ...(n.ppe ? { ppe: true } : {}) };
}

/** A stable key for effect dependencies ("" when nothing is narrowed). */
export const feedNarrowKey = (n: FeedNarrow | null): string => (n ? `${n.label ?? ""}|${n.ppe ? "ppe" : ""}` : "");

/** The muted line under the pills when they hide something ("Showing vehicles only"); null at the defaults. */
export function filterSummary(f: ClassFilter): string | null {
  if (!f.person && !f.vehicle) return "People and vehicles hidden";
  if (f.ppe) return f.person ? "Showing PPE violations only" : "Showing vehicle PPE violations only";
  if (!f.person) return "Showing vehicles only";
  if (!f.vehicle) return "Showing people only";
  return null;
}
