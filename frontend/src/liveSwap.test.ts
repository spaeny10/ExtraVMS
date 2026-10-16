import { describe, expect, it } from "vitest";
import { SWAP_TIMEOUT_MS, type SwapEvent, type SwapState, swapInit, swapReducer, swapStreams } from "./liveSwap";

const play = (s: SwapState, ...events: SwapEvent[]) => events.reduce(swapReducer, s);
const playing = (path: string) => play(swapInit(path, 0), { type: "frame", id: 1 });

describe("liveSwap", () => {
  it("the next path connects hidden, then takes over on its first frame (same key: no reconnect)", () => {
    let s = play(playing("cam_sub"), { type: "path", path: "cam", now: 1000 });
    expect(swapStreams(s).map((x) => [x.id, x.path, x.hidden])).toEqual([[1, "cam_sub", false], [2, "cam", true]]);
    s = swapReducer(s, { type: "frame", id: 2 });
    expect(swapStreams(s).map((x) => [x.id, x.path, x.hidden, x.framed])).toEqual([[2, "cam", false, true]]);   // the old one unmounts
    expect(s.failed).toBeNull();
  });
  it("a next stream that fails or shows nothing for 10 s is dropped; the old path stays and is reported", () => {
    const s0 = play(playing("cam_sub"), { type: "path", path: "cam", now: 1000 });
    const failed = swapReducer(s0, { type: "fail", id: 2 });
    expect(swapStreams(failed).map((x) => x.path)).toEqual(["cam_sub"]);
    expect(failed.failed).toBe("cam");
    expect(swapReducer(s0, { type: "tick", now: 1000 + SWAP_TIMEOUT_MS - 1 })).toBe(s0);
    const late = swapReducer(s0, { type: "tick", now: 1000 + SWAP_TIMEOUT_MS });
    expect(late.next).toBeNull();
    expect(late.failed).toBe("cam");
    // asking for the failed path again changes nothing until the caller goes back to what plays
    expect(swapReducer(late, { type: "path", path: "cam", now: 20_000 })).toBe(late);
    const back = swapReducer(late, { type: "path", path: "cam_sub", now: 20_000 });
    expect(back.failed).toBeNull();
    // ...after which the path may be tried again
    expect(swapReducer(back, { type: "path", path: "cam", now: 21_000 }).next?.path).toBe("cam");
  });
  it("events from a stream that is gone are ignored", () => {
    const s0 = play(playing("cam_sub"), { type: "path", path: "cam", now: 1000 }, { type: "path", path: "cam_sub", now: 1500 });
    expect(s0.next).toBeNull();   // changed its mind: the next stream is dropped
    expect(swapReducer(s0, { type: "frame", id: 2 })).toBe(s0);
    expect(swapReducer(s0, { type: "fail", id: 2 })).toBe(s0);
    expect(swapReducer(s0, { type: "fail", id: 1 })).toBe(s0);   // the one on screen has its own fallback (LivePlayer)
  });
  it("switching again mid-swap replaces the next stream", () => {
    const s = play(playing("a_sub"), { type: "path", path: "a", now: 1000 }, { type: "path", path: "a_hd", now: 1200 });
    expect(swapStreams(s).map((x) => [x.id, x.path])).toEqual([[1, "a_sub"], [3, "a_hd"]]);
  });
  it("a stream that never showed a frame is replaced at once (nothing to keep on screen)", () => {
    const s = swapReducer(swapInit("cam", 0), { type: "path", path: "cam_sub", now: 500 });
    expect(swapStreams(s).map((x) => [x.id, x.path, x.hidden])).toEqual([[2, "cam_sub", false]]);
  });
});
