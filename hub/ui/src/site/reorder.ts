/** List reordering shared by the Contacts and Procedures editors (drag a row onto another, or ▲/▼). Pure, unit-tested. */

/** `list` with the item at `from` moved to index `to` (clamped); the same array when nothing moves. */
export function moveItem<T>(list: T[], from: number, to: number): T[] {
  const t = Math.max(0, Math.min(list.length - 1, to));
  if (from < 0 || from >= list.length || from === t) return list;
  const out = list.slice();
  const [x] = out.splice(from, 1);
  out.splice(t, 0, x);
  return out;
}

/** Rewrite `order` as 0…n-1 in list order, which is what the hub stores. */
export const renumber = <T extends { order: number }>(list: T[]): T[] => list.map((x, i) => (x.order === i ? x : { ...x, order: i }));
