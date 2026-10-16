/**
 * When a live list must be read again from the server instead of trusting its socket: a page left in the background
 * (a phone asleep, a hidden tab) misses live updates — the server restarts, the socket drops and reconnects — and
 * the cards it holds stay "Waiting for verification…" / "Qwen is writing a synopsis…" for hours although the
 * servers finished long ago (seen on the hub's Site Live, 2026-10-15). `useResync` ticks when the page is shown
 * again after being hidden for a while, when the network comes back, and every few minutes while it's visible.
 */
import { useEffect, useState } from "react";

export const RESYNC_HIDDEN_MS = 20_000;       // hidden at least this long: read again when shown
export const RESYNC_EVERY_MS = 5 * 60_000;    // and on this interval while visible, as a safety net

/** Whether coming back after `hiddenFor` ms should trigger a reload (pure, for tests). */
export const shouldResync = (hiddenFor: number) => hiddenFor >= RESYNC_HIDDEN_MS;

/** A counter that goes up whenever the caller should reload its list (0 = never yet). */
export function useResync(): number {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let hiddenAt = document.visibilityState === "hidden" ? Date.now() : 0;
    const bump = () => setTick((t) => t + 1);
    const onVisibility = () => {
      if (document.visibilityState === "hidden") { hiddenAt = Date.now(); return; }
      if (hiddenAt && shouldResync(Date.now() - hiddenAt)) bump();
      hiddenAt = 0;
    };
    const timer = setInterval(() => { if (document.visibilityState === "visible") bump(); }, RESYNC_EVERY_MS);
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("online", bump);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("online", bump);
    };
  }, []);
  return tick;
}
