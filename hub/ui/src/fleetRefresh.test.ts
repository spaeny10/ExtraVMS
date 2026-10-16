import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { REFRESH_DEBOUNCE_MS, REFRESH_FALLBACK_MS, refreshScheduler, wantsRefresh } from "./fleetRefresh";

describe("wantsRefresh", () => {
  it("status changes only, not events", () => {
    expect(wantsRefresh({ type: "site_online" })).toBe(true);
    expect(wantsRefresh({ type: "site_offline" })).toBe(true);
    expect(wantsRefresh({ type: "server_status" })).toBe(true);
    expect(wantsRefresh({ type: "event" })).toBe(false);
    expect(wantsRefresh({ type: "event_removed" })).toBe(false);
  });
});

describe("refreshScheduler", () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });

  it("a burst of pokes makes one load, a second after the first", () => {
    const load = vi.fn();
    const s = refreshScheduler(load);
    s.start();
    s.poke();
    vi.advanceTimersByTime(400);
    s.poke();
    s.poke();
    vi.advanceTimersByTime(REFRESH_DEBOUNCE_MS - 401);
    expect(load).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(load).toHaveBeenCalledTimes(1);
    s.poke();   // a later change: its own load
    vi.advanceTimersByTime(REFRESH_DEBOUNCE_MS);
    expect(load).toHaveBeenCalledTimes(2);
  });

  it("a steady stream of changes still loads once per debounce window", () => {
    const load = vi.fn();
    const s = refreshScheduler(load);
    s.start();
    for (let i = 0; i < 50; i++) { s.poke(); vi.advanceTimersByTime(100); }   // 5 s of pokes every 100 ms
    expect(load).toHaveBeenCalledTimes(5);
  });

  it("falls back to a load every 120 s, counted from the last load", () => {
    const load = vi.fn();
    const s = refreshScheduler(load);
    s.start();
    vi.advanceTimersByTime(REFRESH_FALLBACK_MS - 1);
    expect(load).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(load).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(60_000);
    s.poke();
    vi.advanceTimersByTime(REFRESH_DEBOUNCE_MS);
    expect(load).toHaveBeenCalledTimes(2);
    vi.advanceTimersByTime(REFRESH_FALLBACK_MS - REFRESH_DEBOUNCE_MS);   // not 120 s after the previous fallback
    expect(load).toHaveBeenCalledTimes(2);
    vi.advanceTimersByTime(REFRESH_DEBOUNCE_MS);
    expect(load).toHaveBeenCalledTimes(3);
  });

  it("stop cancels a pending load and the fallback", () => {
    const load = vi.fn();
    const s = refreshScheduler(load, { debounceMs: 50, fallbackMs: 1000 });
    s.start();
    s.poke();
    s.stop();
    s.poke();
    vi.advanceTimersByTime(10_000);
    expect(load).not.toHaveBeenCalled();
  });
});
