import { expect, test } from "vitest";
import type { ChatRef } from "./api";
import { refChips, refSummary, refTarget, stripHandles } from "./chatRefs";

const ref = (event_id: number, match: ChatRef["match"], start_ts: number, extra: Partial<ChatRef> = {}): ChatRef =>
  ({ event_id, camera_id: event_id % 2 ? "cam1" : "cam2", start_ts, match, score: match === "this" ? 1 : 0.9, snapshot: true, ...extra });
const names = (id: string) => ({ cam1: "Side Yard", cam2: "East Door" } as Record<string, string>)[id] ?? id;
const fmt = (ts: number) => `t${ts}`;

const refs: ChatRef[] = [
  ref(12, "possible", 300, { score: 0.78 }),
  ref(7, "this", 100),
  ref(9, "same", 200, { score: 0.91 }),
  ref(11, "same", 250, { score: null, via: "plate" }),
  ref(14, "found", 400, { score: null }),
];

test("chips come in time order with match, camera and time", () => {
  const chips = refChips(refs, 7, names, fmt);
  expect(chips.map((c) => c.ref.event_id)).toEqual([7, 9, 11, 12, 14]);
  expect(chips.map((c) => c.label)).toEqual(["This clip", "Same", "Same", "Possible", "Event"]);
  expect(chips[1]).toMatchObject({ camera: "Side Yard", time: "t200", current: false });
  expect(chips[1].title).toContain("similarity 0.91");
  expect(chips[2].title).toContain("same license plate");
  expect(chips[3].title).toContain("possibly the same");
  expect(new Set(chips.map((c) => c.key)).size).toBe(chips.length);
});

test("the event shown now is marked current and a tap on it opens nothing", () => {
  const chips = refChips(refs, 7, names, fmt);
  expect(chips[0].current).toBe(true);
  expect(refTarget(chips[0].ref, 7)).toBeNull();
  expect(refTarget(chips[1].ref, 7)).toBe(9);
  // after switching to event 9, the conversation's own clip (7) is a target and 9 is current
  const after = refChips(refs, 9, names, fmt);
  expect(after.find((c) => c.ref.event_id === 9)?.current).toBe(true);
  expect(refTarget(after[0].ref, 9)).toBe(7);
});

test("a tap selects the event like the viewer's own select path", () => {
  // EventDetail's openRef: the viewer re-keys to the tapped id and remembers where the answer was
  const opened: number[] = [];
  const onOpen = (id: number) => opened.push(id);
  for (const c of refChips(refs, 7, names, fmt)) {
    const t = refTarget(c.ref, 7);
    if (t != null) onOpen(t);
  }
  expect(opened).toEqual([9, 11, 12, 14]);
});

test("summary and empty refs", () => {
  expect(refSummary(refs)).toBe("2 same · 1 possible · 1 found");
  expect(refSummary([ref(1, "this", 1)])).toBe("");
  expect(refChips(undefined, 1, names, fmt)).toEqual([]);
  expect(refChips(null, 1, names, fmt)).toEqual([]);
});

test("event handles copied from the search results are dropped from the answer", () => {
  expect(stripHandles("He came 3 times [#12] [#14], last at 14:02.")).toBe("He came 3 times, last at 14:02.");
  expect(stripHandles("No handles here.")).toBe("No handles here.");
});
