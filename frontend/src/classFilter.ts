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
