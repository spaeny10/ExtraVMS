/**
 * Site and server pages refetch when the hub says something changed instead of polling every 15 s: a server came
 * online or went offline (site_online / site_offline), or which of its cameras stream changed (server_status, from its
 * heartbeat: hub agents.py). Messages in a burst (a Site's servers reconnecting after a hub restart) make one fetch,
 * REFRESH_DEBOUNCE_MS after the first; the page coming back from the background (useResync) and the socket
 * reconnecting fetch too, and REFRESH_FALLBACK_MS after the last fetch one happens anyway, as a safety net.
 * The scheduler is pure (injectable timers) so the debounce and fallback are tested without React.
 */
import { useEffect, useRef } from "react";
import { useResync } from "@site/resync";
import { type FleetMessage, subscribeFleet } from "./api";

export const REFRESH_DEBOUNCE_MS = 1000;
export const REFRESH_FALLBACK_MS = 120_000;
const REFRESH_TYPES = new Set(["site_online", "site_offline", "server_status"]);

/** Does this fleet message mean a status page should refetch? (Events don't: they have their own feeds.) */
export const wantsRefresh = (m: { type: string }) => REFRESH_TYPES.has(m.type);

type Timers = { set: (fn: () => void, ms: number) => unknown; clear: (t: unknown) => void };
const realTimers: Timers = { set: (fn, ms) => setTimeout(fn, ms), clear: (t) => clearTimeout(t as ReturnType<typeof setTimeout>) };

export type RefreshScheduler = { start: () => void; poke: () => void; stop: () => void };

/**
 * `poke()` asks for a load: the first poke schedules one `debounceMs` later and every poke until then joins it (a
 * steady stream of changes still loads once per `debounceMs`, never starves). Each load re-arms the fallback, which
 * loads `fallbackMs` after the last one. `start()` arms the fallback; `stop()` cancels everything.
 */
export function refreshScheduler(load: () => void, { debounceMs = REFRESH_DEBOUNCE_MS, fallbackMs = REFRESH_FALLBACK_MS, timers = realTimers }:
  { debounceMs?: number; fallbackMs?: number; timers?: Timers } = {}): RefreshScheduler {
  let soon: unknown = null;
  let fallback: unknown = null;
  let stopped = false;
  const arm = () => {
    if (fallback !== null) timers.clear(fallback);
    fallback = stopped ? null : timers.set(fire, fallbackMs);
  };
  const fire = () => {
    if (soon !== null) timers.clear(soon);
    soon = null;
    if (stopped) return;
    load();
    arm();
  };
  return {
    start: () => { stopped = false; arm(); },
    poke: () => { if (!stopped && soon === null) soon = timers.set(fire, debounceMs); },
    stop: () => {
      stopped = true;
      if (soon !== null) timers.clear(soon);
      if (fallback !== null) timers.clear(fallback);
      soon = fallback = null;
    },
  };
}

/**
 * Call `load` again whenever the fleet socket of any of `orgs` says a server's status changed (only messages `filter`
 * accepts, e.g. this Site's servers), when the page is shown again after a while, when the socket reconnects (it may
 * have missed some), and every `fallbackMs` as a fallback. The page still does its own first load. The socket is the
 * page's shared one (subscribeFleet).
 */
export function useFleetRefresh(load: () => unknown, { orgs, fallbackMs = REFRESH_FALLBACK_MS, filter }: {
  orgs: string[]; fallbackMs?: number; filter?: (m: FleetMessage) => boolean;
}): void {
  const loadRef = useRef(load);
  loadRef.current = load;
  const filterRef = useRef(filter);
  filterRef.current = filter;
  const sched = useRef<RefreshScheduler | null>(null);
  const key = [...new Set(orgs.filter(Boolean))].sort().join(",");
  useEffect(() => {
    const s = refreshScheduler(() => { void loadRef.current(); }, { fallbackMs });
    sched.current = s;
    s.start();
    const unsubs = (key ? key.split(",") : []).map((org) => {
      let opened = false;
      return subscribeFleet(org, (m) => { if (wantsRefresh(m) && (!filterRef.current || filterRef.current(m))) s.poke(); },
        (up) => { if (up && opened) s.poke(); if (up) opened = true; });
    });
    return () => { s.stop(); unsubs.forEach((u) => u()); if (sched.current === s) sched.current = null; };
  }, [key, fallbackMs]);
  const resync = useResync();
  useEffect(() => { if (resync) sched.current?.poke(); }, [resync]);
}
