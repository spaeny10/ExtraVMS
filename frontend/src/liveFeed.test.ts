import { expect, test } from "vitest";
import type { NvrEvent } from "./api";
import { checkingText, checkingVehicle, feedCards } from "./liveFeed";

const ev = (id: number, camera_class: string, status: string) => ({ id, camera_class, status }) as unknown as NvrEvent;

test("unverified vehicles wait; people and verified vehicles show at once", () => {
  const list = [
    ev(1, "vehicle", "open"), ev(2, "person", "open"), ev(3, "vehicle", "pending"), ev(4, "person", "pending"),
    ev(5, "vehicle", "verified"), ev(6, "person", "verified"), ev(7, "motion", "open"), ev(8, "vehicle", "error"),
  ];
  const { cards, checking } = feedCards(list);
  expect(cards.map((e) => e.id)).toEqual([2, 4, 5, 6, 7, 8]);   // order kept
  expect(checking).toBe(2);
  expect(checkingVehicle(list[0])).toBe(true);
  expect(checkingVehicle(list[4])).toBe(false);
});

test("nothing to check: every card shows and there is no line", () => {
  const { cards, checking } = feedCards([ev(1, "person", "open"), ev(2, "vehicle", "verified")]);
  expect(cards).toHaveLength(2);
  expect(checking).toBe(0);
  expect(checkingText(checking)).toBeNull();
  expect(feedCards([])).toEqual({ cards: [], checking: 0 });
});

test("the muted line counts them", () => {
  expect(checkingText(1)).toBe("Checking 1 vehicle…");
  expect(checkingText(3)).toBe("Checking 3 vehicles…");
});
