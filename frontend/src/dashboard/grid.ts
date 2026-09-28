/**
 * Pure grid maths for the dashboard editor: collisions, pushing overlapped widgets down, compacting upwards,
 * finding a free slot, and the single-column stack used on phones. No React here so it is unit-testable.
 */
export type Box = { id: string; x: number; y: number; w: number; h: number };

export const collides = (a: Box, b: Box): boolean =>
  a.id !== b.id && !(a.x + a.w <= b.x || b.x + b.w <= a.x || a.y + a.h <= b.y || b.y + b.h <= a.y);

const byPosition = <T extends Box>(items: T[]): T[] => [...items].sort((a, b) => a.y - b.y || a.x - b.x);

/** Clamp a box into the grid (width first, then position). */
export function clampBox<T extends Box>(b: T, cols: number, minW = 2, minH = 2): T {
  const w = Math.max(minW, Math.min(cols, b.w));
  const h = Math.max(minH, b.h);
  const x = Math.max(0, Math.min(cols - w, b.x));
  const y = Math.max(0, b.y);
  return { ...b, x, y, w, h };
}

/**
 * Lay the widgets out with `fixedId` where it is: every other widget is pushed down until it overlaps
 * nothing placed before it (in reading order), then everything except the fixed one floats back up as far
 * as it can. Deterministic: the same input always gives the same layout.
 */
export function resolve<T extends Box>(items: T[], fixedId: string | null): T[] {
  const fixed = items.find((i) => i.id === fixedId);
  const placed: T[] = fixed ? [{ ...fixed }] : [];
  for (const it of byPosition(items.filter((i) => i.id !== fixedId))) {
    const b = { ...it };
    while (placed.some((p) => collides(p, b))) b.y += 1;
    placed.push(b);
  }
  const pos = new Map(compact(placed, fixedId).map((b) => [b.id, b]));
  return items.map((i) => pos.get(i.id)!);
}

/** Float widgets up while nothing blocks them (the fixed one stays). Keeps the caller's item identity. */
export function compact<T extends Box>(items: T[], fixedId: string | null = null): T[] {
  const out: T[] = [];
  for (const it of byPosition(items)) {
    const b = { ...it };
    if (b.id !== fixedId) {
      while (b.y > 0 && !out.some((p) => collides(p, { ...b, y: b.y - 1 }))) b.y -= 1;
    }
    out.push(b);
  }
  // give the result back in the caller's order so React keys stay stable
  const pos = new Map(out.map((b) => [b.id, b]));
  return items.map((i) => pos.get(i.id)!);
}

/** First slot (reading order) where a w×h widget fits without overlap. */
export function nextFree(items: Box[], w: number, h: number, cols: number): { x: number; y: number } {
  const ww = Math.min(w, cols);
  const maxY = items.reduce((m, b) => Math.max(m, b.y + b.h), 0);
  for (let y = 0; y <= maxY; y++) {
    for (let x = 0; x + ww <= cols; x++) {
      const probe = { id: "__probe", x, y, w: ww, h };
      if (!items.some((b) => collides(b, probe))) return { x, y };
    }
  }
  return { x: 0, y: maxY };
}

/** Phones: one column, reading order, each widget full width with its own height. */
export function stackForPhone<T extends Box>(items: T[], cols: number): T[] {
  let y = 0;
  return byPosition(items).map((b) => {
    const out = { ...b, x: 0, w: cols, y };
    y += b.h;
    return out;
  });
}
