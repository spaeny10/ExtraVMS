/**
 * The live-stream budget shared by a page's camera tiles (dashboards, the hub's combined Live, the server's Live view):
 * how many may stream at once (MAX_LIVE), and how many may stream HD (MAX_HD). Its own module, not Dashboard.tsx, so
 * Views.tsx can use it without importing the dashboard (which imports Views).
 */
import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";

export const MAX_LIVE = 9;
/** HD streams at once on a page: the automatic SD/HD only goes up while one of these is free (autoQuality.ts); a
 *  viewer's own HD pick always plays but takes a slot too */
export const MAX_HD = 2;

/**
 * Which camera tiles may hold a live stream: the first MAX_LIVE visible ones, or any the viewer pressed Play on.
 * HD slots: a tile streaming HD holds one (holdHd); hdFree says whether a tile may go up (it holds one, or fewer than
 * MAX_HD are held).
 */
export type Budget = {
  playing: (id: string) => boolean; visible: (id: string, on: boolean) => void; force: (id: string) => void;
  hdFree: (id: string) => boolean; holdHd: (id: string, on: boolean) => void;
};
// no provider (a page without a budget): everything plays, HD unlimited
export const BudgetCtx = createContext<Budget>({ playing: () => true, visible: () => {}, force: () => {}, hdFree: () => true, holdHd: () => {} });
export const useBudget = () => useContext(BudgetCtx);

export function useBudgetProvider(): Budget {
  const [order, setOrder] = useState<string[]>([]);
  const [hd, setHd] = useState<string[]>([]);
  const visible = useCallback((id: string, on: boolean) => setOrder((o) => (on ? (o.includes(id) ? o : [...o, id]) : o.filter((x) => x !== id))), []);
  const force = useCallback((id: string) => setOrder((o) => [id, ...o.filter((x) => x !== id)]), []);
  const playing = useCallback((id: string) => { const i = order.indexOf(id); return i >= 0 && i < MAX_LIVE; }, [order]);
  const holdHd = useCallback((id: string, on: boolean) => setHd((h) => (on ? (h.includes(id) ? h : [...h, id]) : h.includes(id) ? h.filter((x) => x !== id) : h)), []);
  const hdFree = useCallback((id: string) => hd.includes(id) || hd.length < MAX_HD, [hd]);
  return useMemo(() => ({ playing, visible, force, hdFree, holdHd }), [playing, visible, force, hdFree, holdHd]);
}

/** The live-stream budget for any grid of live tiles outside a Dashboard (e.g. the hub's combined Live). */
export function LiveBudgetProvider({ children }: { children: ReactNode }) {
  return <BudgetCtx.Provider value={useBudgetProvider()}>{children}</BudgetCtx.Provider>;
}
