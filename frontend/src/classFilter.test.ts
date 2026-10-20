import { expect, test } from "vitest";
import { DEFAULT_CLASS_FILTER, classPass, feedNarrow, feedNarrowKey, filterSummary, hubFeedQuery, isPpeViolation, nvrFeedQuery, parseClassFilter } from "./classFilter";

const ppeRule = { kind: "ppe" };
const towRule = { kind: "towing" };

test("defaults: People and Vehicles on, PPE off (the Timeline's)", () => {
  expect(DEFAULT_CLASS_FILTER).toEqual({ person: true, vehicle: true, ppe: false });
});

test("a PPE violation: the Timeline's ppe column or the event's broken rule", () => {
  expect(isPpeViolation({ ppe: 1 })).toBe(true);
  expect(isPpeViolation({ ppe: 0 })).toBe(false);
  expect(isPpeViolation({ ppe: null })).toBe(false);
  expect(isPpeViolation({})).toBe(false);
  expect(isPpeViolation({ policy: ppeRule })).toBe(true);
  expect(isPpeViolation({ policy: towRule })).toBe(false);
  expect(isPpeViolation({ policy: null })).toBe(false);
});

test("the pills: people and vehicles by their own, other labels always, PPE on keeps violations only", () => {
  const f = DEFAULT_CLASS_FILTER;
  expect(classPass({ camera_class: "person" }, f)).toBe(true);
  expect(classPass({ camera_class: "vehicle" }, f)).toBe(true);
  expect(classPass({ camera_class: "person" }, { ...f, person: false })).toBe(false);
  expect(classPass({ camera_class: "vehicle" }, { ...f, vehicle: false })).toBe(false);
  expect(classPass({ camera_class: "animal" }, { person: false, vehicle: false, ppe: false })).toBe(true);
  const ppeOnly = { ...f, ppe: true };
  expect(classPass({ camera_class: "person" }, ppeOnly)).toBe(false);
  expect(classPass({ camera_class: "person", policy: towRule }, ppeOnly)).toBe(false);
  expect(classPass({ camera_class: "person", policy: ppeRule }, ppeOnly)).toBe(true);
  expect(classPass({ camera_class: "person", ppe: 1 }, ppeOnly)).toBe(true);
  // People off wins over a PPE violation
  expect(classPass({ camera_class: "person", ppe: 1 }, { ...ppeOnly, person: false })).toBe(false);
});

test("a stored choice: bad JSON, nothing stored or odd values fall back to the defaults", () => {
  expect(parseClassFilter(null)).toEqual(DEFAULT_CLASS_FILTER);
  expect(parseClassFilter(undefined)).toEqual(DEFAULT_CLASS_FILTER);
  expect(parseClassFilter("{not json")).toEqual(DEFAULT_CLASS_FILTER);
  expect(parseClassFilter("[1,2]")).toEqual(DEFAULT_CLASS_FILTER);
  expect(parseClassFilter("42")).toEqual(DEFAULT_CLASS_FILTER);
  expect(parseClassFilter('{"person":"no","ppe":1}')).toEqual(DEFAULT_CLASS_FILTER);
  expect(parseClassFilter('{"vehicle":false}')).toEqual({ person: true, vehicle: false, ppe: false });
  expect(parseClassFilter('{"person":false,"vehicle":false,"ppe":true,"extra":1}')).toEqual({ person: false, vehicle: false, ppe: true });
  // the defaults are not handed out to be mutated
  parseClassFilter("{bad").person = false;
  expect(DEFAULT_CLASS_FILTER.person).toBe(true);
});

test("the pills as fetch params: one class narrows to it, PPE asks for violations, the defaults narrow nothing", () => {
  const f = DEFAULT_CLASS_FILTER;
  expect(feedNarrow(f)).toBeNull();
  expect(nvrFeedQuery(feedNarrow(f))).toEqual({});
  expect(hubFeedQuery(feedNarrow(f))).toEqual({});
  expect(feedNarrowKey(feedNarrow(f))).toBe("");

  const vehicles = feedNarrow({ ...f, person: false });
  expect(vehicles).toEqual({ label: "vehicle", ppe: false });
  expect(nvrFeedQuery(vehicles)).toEqual({ label: "vehicle" });
  expect(hubFeedQuery(vehicles)).toEqual({ classes: ["vehicle"] });

  const people = feedNarrow({ ...f, vehicle: false });
  expect(nvrFeedQuery(people)).toEqual({ label: "person" });
  expect(hubFeedQuery(people)).toEqual({ classes: ["person"] });

  const ppe = feedNarrow({ ...f, ppe: true });
  expect(nvrFeedQuery(ppe)).toEqual({ flags: ["ppe"] });
  expect(hubFeedQuery(ppe)).toEqual({ ppe: true });
  expect(nvrFeedQuery(feedNarrow({ person: true, vehicle: false, ppe: true }))).toEqual({ label: "person", flags: ["ppe"] });
  expect(hubFeedQuery(feedNarrow({ person: true, vehicle: false, ppe: true }))).toEqual({ classes: ["person"], ppe: true });

  // both classes off: only other labels pass, which no class param can ask for
  expect(feedNarrow({ person: false, vehicle: false, ppe: false })).toBeNull();
  // a change of pills changes the key (the fetches' dependency)
  const keys = new Set([vehicles, people, ppe, null].map(feedNarrowKey));
  expect(keys.size).toBe(4);
});

test("the line under the pills says what they hide; the defaults say nothing", () => {
  const f = DEFAULT_CLASS_FILTER;
  expect(filterSummary(f)).toBeNull();
  expect(filterSummary({ ...f, person: false })).toBe("Showing vehicles only");
  expect(filterSummary({ ...f, vehicle: false })).toBe("Showing people only");
  expect(filterSummary({ ...f, ppe: true })).toBe("Showing PPE violations only");
  expect(filterSummary({ person: true, vehicle: false, ppe: true })).toBe("Showing PPE violations only");
  expect(filterSummary({ person: false, vehicle: true, ppe: true })).toBe("Showing vehicle PPE violations only");
  expect(filterSummary({ person: false, vehicle: false, ppe: false })).toBe("People and vehicles hidden");
});
