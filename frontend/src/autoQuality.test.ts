import { describe, expect, it } from "vitest";
import { type AutoInput, COOLDOWN_MS, MAX_SAMPLES, MIN_GAP_MS, type TimedSample, badLink, decideQuality, healthyFor, pushSample } from "./autoQuality";
import { statsSample } from "./whepTiming";

/** samples every 2 s ending at `end`: 300 packets and 30 frames each, with the given loss / freezes / dropped per sample */
function run(end: number, n: number, bad: Partial<{ lost: number; freezes: number; dropped: number }> = {}): TimedSample[] {
  return Array.from({ length: n }, (_, k) => ({
    at: end - (n - 1 - k) * 2000, packetsReceived: 300, packetsLost: bad.lost ?? 0, freezes: bad.freezes ?? 0,
    framesDecoded: 30, framesDropped: bad.dropped ?? 0,
  }));
}

const base = (over: Partial<AutoInput> = {}): AutoInput => ({
  width: 900, focused: false, hdDecodable: true, sdDecodable: true, hdSlotFree: true, samples: run(100_000, 5), now: 100_000,
  current: "sd", lastSwitch: null, lastDrop: null, ...over,
});

describe("decideQuality: up to HD", () => {
  it("a wide, clean tile goes HD", () => {
    expect(decideQuality(base())).toEqual({ quality: "hd", reason: "big", badLink: false });
  });
  it("a focused tile goes HD however narrow", () => {
    expect(decideQuality(base({ width: 400, focused: true }))?.quality).toBe("hd");
    expect(decideQuality(base({ width: 719 }))).toBeNull();
  });
  it("needs 5 s of clean link first", () => {
    expect(decideQuality(base({ samples: run(100_000, 2) }))).toBeNull();       // 2 samples: 2 s of history
    expect(decideQuality(base({ samples: run(100_000, 4) }))?.quality).toBe("hd");   // 6 s: the window is covered
    expect(decideQuality(base({ samples: [] }))).toBeNull();
    // one lossy, freezing or dropping sample in the window holds it back
    for (const bad of [{ lost: 4 }, { freezes: 1 }, { dropped: 1 }]) {
      const s = run(100_000, 5);
      s[4] = run(100_000, 1, bad)[0];
      expect(decideQuality(base({ samples: s })), JSON.stringify(bad)).toBeNull();
    }
    // stalled (nothing decoded) is not clean either
    expect(decideQuality(base({ samples: run(100_000, 5).map((x) => ({ ...x, framesDecoded: 0, packetsReceived: 0 })) }))).toBeNull();
  });
  it("not when HD can't play here, or the page's HD slots are taken", () => {
    expect(decideQuality(base({ hdDecodable: false }))).toBeNull();
    expect(decideQuality(base({ hdSlotFree: false }))).toBeNull();
  });
  it("waits 10 s after any switch and 30 s after a drop for a bad link", () => {
    expect(decideQuality(base({ lastSwitch: 100_000 - MIN_GAP_MS + 1 }))).toBeNull();
    expect(decideQuality(base({ lastSwitch: 100_000 - MIN_GAP_MS }))?.quality).toBe("hd");
    expect(decideQuality(base({ lastSwitch: 80_000, lastDrop: 100_000 - COOLDOWN_MS + 1 }))).toBeNull();
    expect(decideQuality(base({ lastSwitch: 70_000, lastDrop: 100_000 - COOLDOWN_MS }))?.quality).toBe("hd");
  });
  it("an SD stream this browser can't play goes straight to a playable HD (no stats to wait for)", () => {
    expect(decideQuality(base({ sdDecodable: false, samples: [], width: 300, hdSlotFree: false, lastSwitch: 99_000 })))
      .toEqual({ quality: "hd", reason: "sd-unplayable", badLink: false });
    expect(decideQuality(base({ sdDecodable: false, hdDecodable: false }))).toBeNull();
  });
});

describe("decideQuality: down to SD", () => {
  const hd = (over: Partial<AutoInput> = {}) => base({ current: "hd", lastSwitch: 50_000, ...over });
  it("a clean, wide HD tile stays", () => {
    expect(decideQuality(hd())).toBeNull();
  });
  it("loss over 3%, two freezes in 10 s, or over 8% dropped frames: down, starting the cooldown", () => {
    expect(decideQuality(hd({ samples: run(100_000, 5, { lost: 10 }) }))).toEqual({ quality: "sd", reason: "link", badLink: true });   // 3.2%
    expect(decideQuality(hd({ samples: run(100_000, 5, { lost: 9 }) }))).toBeNull();                                                   // 2.9%
    const frozen = run(100_000, 5);
    frozen[2] = { ...frozen[2], freezes: 1 };
    frozen[4] = { ...frozen[4], freezes: 1 };
    expect(decideQuality(hd({ samples: frozen }))?.reason).toBe("link");
    frozen[2] = { ...frozen[2], at: 89_000 };   // the first freeze is older than 10 s: one in the window
    expect(decideQuality(hd({ samples: frozen }))).toBeNull();
    expect(decideQuality(hd({ samples: run(100_000, 5, { dropped: 3 }) }))?.reason).toBe("link");   // 3/33 = 9%
    expect(decideQuality(hd({ samples: run(100_000, 5, { dropped: 2 }) }))).toBeNull();              // 6%
  });
  it("a tile that shrank goes down unless focused (no cooldown: growing again may go up)", () => {
    expect(decideQuality(hd({ width: 500 }))).toEqual({ quality: "sd", reason: "small", badLink: false });
    expect(decideQuality(hd({ width: 500, focused: true }))).toBeNull();
  });
  it("no switch within 10 s of the last, except when HD can't play at all", () => {
    expect(decideQuality(hd({ width: 500, lastSwitch: 95_000 }))).toBeNull();
    expect(decideQuality(hd({ samples: run(100_000, 5, { lost: 50 }), lastSwitch: 95_000 }))).toBeNull();
    expect(decideQuality(hd({ hdDecodable: false, lastSwitch: 99_000 }))?.reason).toBe("hd-unplayable");
  });
  it("stays on HD when SD can't play here", () => {
    expect(decideQuality(hd({ sdDecodable: false, width: 300, samples: run(100_000, 5, { lost: 50 }) }))).toBeNull();
  });
});

describe("samples", () => {
  it("healthyFor needs history reaching back to the window's start", () => {
    expect(healthyFor(run(10_000, 3), 10_000)).toBe(false);   // 6, 8, 10 s: nothing at or before 5 s
    expect(healthyFor(run(10_000, 3), 11_000)).toBe(true);    // the 6 s sample marks the window's start
    expect(healthyFor(run(10_000, 4), 10_000)).toBe(true);
    expect(healthyFor(run(10_000, 4), 16_000)).toBe(false);   // stats stopped: nothing in the last 5 s
  });
  it("badLink ignores an empty window", () => {
    expect(badLink([], 0)).toBe(false);
  });
  it("pushSample keeps the newest few, from onStats samples", () => {
    let xs: TimedSample[] = [];
    for (let k = 0; k < MAX_SAMPLES + 3; k++) {
      xs = pushSample(xs, statsSample({ packetsReceived: k * 100, framesDecoded: k * 10 }, { packetsReceived: (k + 1) * 100, framesDecoded: (k + 1) * 10 }), k * 2000);
    }
    expect(xs).toHaveLength(MAX_SAMPLES);
    expect(xs[0].at).toBe(3 * 2000);
    expect(xs[xs.length - 1]).toEqual({ at: (MAX_SAMPLES + 2) * 2000, packetsLost: 0, packetsReceived: 100, freezes: 0, framesDropped: 0, framesDecoded: 10 });
  });
});
