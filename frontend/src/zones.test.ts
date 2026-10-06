import { expect, test } from "vitest";
import { newZone, ppeBadge, ppeItemsText, retypeZone, togglePpeItem, zoneAllowed, type Zone } from "./api";

const SQ: [number, number][] = [[0, 0.5], [1, 0.5], [1, 1], [0, 1]];

test("a new PPE zone requires both items and serializes like the backend expects", () => {
  const z = newZone("ppe", [{ name: "PPE zone 1", type: "ppe", points: SQ }], SQ);
  expect(z).toEqual({ name: "PPE zone 2", type: "ppe", points: SQ, required: ["hard_hat", "vest"] });
  expect(JSON.parse(JSON.stringify(z)).required).toEqual(["hard_hat", "vest"]);
  expect(newZone("exclude", [], SQ)).toEqual({ name: "Mask 1", type: "exclude", points: SQ });
});

test("required items keep the backend order and other types drop the PPE fields", () => {
  let z: Zone = newZone("ppe", [], SQ);
  z = togglePpeItem(z, "hard_hat", false);
  expect(z.required).toEqual(["vest"]);
  z = togglePpeItem(z, "hard_hat", true);
  expect(z.required).toEqual(["hard_hat", "vest"]);
  const area = retypeZone({ ...z, min_dwell_s: 8 }, "area");
  expect(area).toEqual({ name: "PPE zone 1", type: "area", points: SQ });
  expect(retypeZone(area, "ppe").required).toEqual(["hard_hat", "vest"]);
  expect(retypeZone({ ...z, required: ["vest"], min_dwell_s: 8 }, "ppe")).toMatchObject({ required: ["vest"], min_dwell_s: 8 });
});

test("a PPE zone never filters detections", () => {
  const zones: Zone[] = [newZone("ppe", [], SQ)];
  expect(zoneAllowed(0.5, 0.2, zones)).toBe(true);
  expect(zoneAllowed(0.5, 0.8, zones)).toBe(true);
});

test("PPE badge and item text", () => {
  expect(ppeBadge({ kind: "ppe", tags: ["ppe violation", "no hard hat", "no hi-vis vest"] })).toBe("No hard hat / hi-vis vest");
  expect(ppeBadge({ kind: "entry" })).toBeNull();
  expect(ppeItemsText({ hard_hat: "missing", vest: "present" }, ["hard_hat", "vest"])).toBe("Hard hat missing · Hi-vis vest worn");
  expect(ppeItemsText(null, ["vest"])).toBe("Hi-vis vest unclear");
});
