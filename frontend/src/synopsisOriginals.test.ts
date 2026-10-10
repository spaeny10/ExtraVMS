import { expect, test } from "vitest";
import type { NvrEvent, Synopsis, WeaponCheck } from "./api";
import { synopsisOriginals } from "./EventDetail";

const syn = (summary: string, extra: Partial<Synopsis> = {}): Synopsis =>
  ({ summary, objects: [], activity: "", threat_level: "low", tags: [], ...extra });
const check = (verdict: WeaponCheck["verdict"], original?: Synopsis): WeaponCheck & { original?: Synopsis } =>
  ({ confirmed: verdict === "confirmed", verdict, persons: [], reason: "r", ...(original ? { original } : {}) });
const claim = syn("holding a black handgun", { threat_level: "high" });

test("the weapon check's original is read-only, never a correction to revert to", () => {
  const e = { synopsis_json: syn("holding a black towel", { weapon_check: check("not_confirmed", claim) }), synopsis_original: null, corrected_at: null } as NvrEvent;
  expect(synopsisOriginals(e)).toEqual({ correction: null, weaponClaim: claim });
});

test("older events: synopsis_original without corrected_at was the weapon check's, not an operator's", () => {
  const e = { synopsis_json: syn("holding a black towel", { weapon_check: check("not_confirmed") }), synopsis_original: claim, corrected_at: null } as NvrEvent;
  expect(synopsisOriginals(e)).toEqual({ correction: null, weaponClaim: claim });
});

test("an operator correction still offers Revert to the model's version", () => {
  const model = syn("a man walked in", { threat_level: "medium" });
  const e = { synopsis_json: syn("the owner walked in"), synopsis_original: model, corrected_at: 1700000000 } as NvrEvent;
  expect(synopsisOriginals(e)).toEqual({ correction: model, weaponClaim: null });
});
