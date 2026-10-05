import { describe, expect, it } from "vitest";
import { moveItem, renumber } from "./reorder";

describe("moveItem", () => {
  const l = ["a", "b", "c", "d"];
  it("moves up, down and clamps", () => {
    expect(moveItem(l, 2, 0)).toEqual(["c", "a", "b", "d"]);
    expect(moveItem(l, 0, 2)).toEqual(["b", "c", "a", "d"]);
    expect(moveItem(l, 3, 9)).toBe(l);
    expect(moveItem(l, 0, -1)).toBe(l);
    expect(moveItem(l, 7, 0)).toBe(l);
  });
  it("renumbers order", () => {
    expect(renumber([{ order: 5 }, { order: 0 }])).toEqual([{ order: 0 }, { order: 1 }]);
  });
});
