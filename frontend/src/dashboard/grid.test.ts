import { expect, test } from "vitest";
import { clampBox, collides, compact, nextFree, resolve, stackForPhone } from "./grid";

const box = (id: string, x: number, y: number, w: number, h: number) => ({ id, x, y, w, h });

test("collision is by overlap, never with itself", () => {
  const a = box("a", 0, 0, 4, 4);
  expect(collides(a, box("b", 3, 3, 2, 2))).toBe(true);
  expect(collides(a, box("b", 4, 0, 2, 2))).toBe(false);
  expect(collides(a, box("b", 0, 4, 2, 2))).toBe(false);
  expect(collides(a, { ...a })).toBe(false);
});

test("clampBox keeps a widget inside 12 columns with a minimum size", () => {
  expect(clampBox(box("a", 10, 0, 4, 1), 12)).toEqual(box("a", 8, 0, 4, 2));
  expect(clampBox(box("a", -3, -2, 30, 3), 12)).toEqual(box("a", 0, 0, 12, 3));
});

test("resolve pushes overlapped widgets down and floats the rest back up", () => {
  const items = [box("a", 0, 0, 4, 4), box("b", 0, 4, 4, 4), box("c", 4, 0, 4, 2)];
  // move "a" down onto "b": b goes below a, c stays where it is
  const out = resolve(items.map((i) => (i.id === "a" ? { ...i, y: 2 } : i)), "a");
  const by = Object.fromEntries(out.map((b) => [b.id, b]));
  expect(by.a.y).toBe(2);
  expect(by.b.y).toBe(6);
  expect(by.c).toEqual(box("c", 4, 0, 4, 2));
  // order of the array is preserved for stable keys
  expect(out.map((b) => b.id)).toEqual(["a", "b", "c"]);
});

test("resolve cascades pushes through a column", () => {
  const items = [box("a", 0, 0, 4, 2), box("b", 0, 2, 4, 2), box("c", 0, 4, 4, 2)];
  const out = resolve(items.map((i) => (i.id === "a" ? { ...i, h: 5 } : i)), "a");
  const by = Object.fromEntries(out.map((b) => [b.id, b]));
  expect([by.a.y, by.b.y, by.c.y]).toEqual([0, 5, 7]);
});

test("compact removes gaps left by a deleted widget", () => {
  const out = compact([box("a", 0, 6, 4, 2), box("b", 4, 3, 4, 2)]);
  expect(out.every((b) => b.y === 0)).toBe(true);
});

test("nextFree finds the first slot in reading order", () => {
  const items = [box("a", 0, 0, 8, 4), box("b", 8, 0, 4, 2)];
  expect(nextFree(items, 4, 2, 12)).toEqual({ x: 8, y: 2 });
  expect(nextFree(items, 12, 2, 12)).toEqual({ x: 0, y: 4 });
  expect(nextFree([], 4, 4, 12)).toEqual({ x: 0, y: 0 });
});

test("stackForPhone lays widgets out in one full-width column in reading order", () => {
  const out = stackForPhone([box("b", 4, 0, 4, 3), box("a", 0, 0, 4, 2), box("c", 0, 2, 8, 1)], 12);
  expect(out.map((b) => b.id)).toEqual(["a", "b", "c"]);
  expect(out.map((b) => [b.x, b.w, b.y])).toEqual([[0, 12, 0], [0, 12, 2], [0, 12, 5]]);
});
