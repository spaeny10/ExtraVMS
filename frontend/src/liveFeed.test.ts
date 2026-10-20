import { expect, test, vi } from "vitest";
import type { NvrEvent } from "./api";
import { DEFAULT_CLASS_FILTER, FEED_FILTER_KEY, checkingText, checkingVehicle, feedCards, loadFeedFilter, saveFeedFilter } from "./liveFeed";

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
  expect(feedCards([])).toEqual({ cards: [], checking: 0, hidden: 0 });
});

test("the muted line counts them", () => {
  expect(checkingText(1)).toBe("Checking 1 vehicle…");
  expect(checkingText(3)).toBe("Checking 3 vehicles…");
});

test("the pills filter the cards; the checking line follows the Vehicles pill; hidden counts what they left out", () => {
  const list = [
    { ...ev(1, "person", "verified"), policy: { kind: "ppe" } } as unknown as NvrEvent,
    ev(2, "person", "open"), ev(3, "vehicle", "verified"), ev(4, "vehicle", "open"), ev(5, "animal", "verified"),
  ];
  expect(feedCards(list)).toMatchObject({ checking: 1, hidden: 0 });
  let r = feedCards(list, { person: true, vehicle: false, ppe: false });
  expect(r.cards.map((e) => e.id)).toEqual([1, 2, 5]);
  expect(r.checking).toBe(0);   // Vehicles off: no "Checking 1 vehicle…"
  expect(r.hidden).toBe(1);
  r = feedCards(list, { person: false, vehicle: true, ppe: false });
  expect(r.cards.map((e) => e.id)).toEqual([3, 5]);
  expect(r.checking).toBe(1);
  r = feedCards(list, { person: true, vehicle: true, ppe: true });
  expect(r.cards.map((e) => e.id)).toEqual([1]);   // PPE violations only (Timeline's predicate)
  expect(r.checking).toBe(0);
  expect(r.hidden).toBe(3);
  r = feedCards([ev(1, "person", "verified")], { person: false, vehicle: true, ppe: false });
  expect(r).toEqual({ cards: [], checking: 0, hidden: 1 });   // the column says "Nothing matches these filters"
});

test("the viewer's pills: defaults without storage, bad JSON, and a round trip under their own key", () => {
  const store = new Map<string, string>();
  try {
    // blocked site data: the accessor throws, the pills still work on the defaults
    const blocked = () => { throw new Error("SecurityError"); };
    vi.stubGlobal("localStorage", { getItem: blocked, setItem: blocked });
    expect(loadFeedFilter()).toEqual(DEFAULT_CLASS_FILTER);
    expect(() => saveFeedFilter({ person: false, vehicle: true, ppe: true })).not.toThrow();
    vi.stubGlobal("localStorage", { getItem: (k: string) => store.get(k) ?? null, setItem: (k: string, v: string) => void store.set(k, v) });
    expect(loadFeedFilter()).toEqual(DEFAULT_CLASS_FILTER);
    store.set(FEED_FILTER_KEY, "{oops");
    expect(loadFeedFilter()).toEqual(DEFAULT_CLASS_FILTER);
    saveFeedFilter({ person: false, vehicle: true, ppe: true });
    expect(loadFeedFilter()).toEqual({ person: false, vehicle: true, ppe: true });
    expect(FEED_FILTER_KEY).not.toBe("timelineFilter");
    expect(store.has("timelineFilter")).toBe(false);
  } finally {
    vi.unstubAllGlobals();
  }
});
