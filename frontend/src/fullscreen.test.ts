import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

type Mod = typeof import("./fullscreen");

/** A stand-in for the page: document (full screen element and its events), screen.orientation and matchMedia. */
function page(o: { touch?: boolean; portrait?: boolean; lockFails?: boolean; unlockThrows?: boolean; noOrientation?: boolean } = {}) {
  const listeners = new Map<string, Set<() => void>>();
  const doc = {
    fullscreenElement: null as unknown,
    fullscreenEnabled: true,
    addEventListener: (t: string, f: () => void) => { if (!listeners.has(t)) listeners.set(t, new Set()); listeners.get(t)!.add(f); },
    removeEventListener: (t: string, f: () => void) => { listeners.get(t)?.delete(f); },
    exitFullscreen: vi.fn(async () => { doc.fullscreenElement = null; fire("fullscreenchange"); }),
  };
  const fire = (t: string) => [...(listeners.get(t) ?? [])].forEach((f) => f());
  const orientation = {
    type: o.portrait === false ? "landscape-primary" : "portrait-primary",
    lock: vi.fn(async (_o: string) => { if (o.lockFails) throw new DOMException("not supported", "NotSupportedError"); }),
    unlock: vi.fn(() => { if (o.unlockThrows) throw new Error("no"); }),
  };
  vi.stubGlobal("document", doc);
  vi.stubGlobal("screen", o.noOrientation ? {} : { orientation });
  vi.stubGlobal("matchMedia", (q: string) => ({ matches: q.includes("coarse") || q.includes("hover: none") ? !!o.touch : q.includes("portrait") ? o.portrait !== false : false }));
  const el = {
    requestFullscreen: vi.fn(async () => { doc.fullscreenElement = el; fire("fullscreenchange"); }),
  };
  /** the viewer leaves full screen by other means (Esc, back) */
  const leave = () => { doc.fullscreenElement = null; fire("fullscreenchange"); };
  return { doc, orientation, el, leave, listeners };
}
const wide = { videoWidth: 2592, videoHeight: 1520 } as unknown as HTMLVideoElement;
const tall = { videoWidth: 1080, videoHeight: 1920 } as unknown as HTMLVideoElement;

let fs: Mod;
beforeEach(async () => {
  vi.resetModules();
  fs = await import("./fullscreen");
});
afterEach(() => { vi.unstubAllGlobals(); });

describe("wantsLandscape", () => {
  it("only a touch device held upright with a picture wider than tall", () => {
    const w = { width: 1920, height: 1080 };
    expect(fs.wantsLandscape({ touch: true, portrait: true, picture: w })).toBe(true);
    expect(fs.wantsLandscape({ touch: false, portrait: true, picture: w })).toBe(false);
    expect(fs.wantsLandscape({ touch: true, portrait: false, picture: w })).toBe(false);
    expect(fs.wantsLandscape({ touch: true, portrait: true, picture: { width: 1080, height: 1920 } })).toBe(false);
    expect(fs.wantsLandscape({ touch: true, portrait: true, picture: { width: 0, height: 0 } })).toBe(false);   // unknown
  });
});

describe("pictureSize", () => {
  it("a video's frames, an image's pixels, else its box", () => {
    expect(fs.pictureSize(wide)).toEqual({ width: 2592, height: 1520 });
    expect(fs.pictureSize({ naturalWidth: 960, naturalHeight: 563 } as unknown as HTMLImageElement)).toEqual({ width: 960, height: 563 });
    expect(fs.pictureSize({ videoWidth: 0, videoHeight: 0, clientWidth: 360, clientHeight: 203 } as unknown as HTMLVideoElement)).toEqual({ width: 360, height: 203 });
    expect(fs.pictureSize(null)).toEqual({ width: 0, height: 0 });
  });
});

describe("enterFullscreen", () => {
  it("a phone held upright: full screen, then the screen locked to landscape for a wide picture", async () => {
    const p = page({ touch: true, portrait: true });
    await expect(fs.enterFullscreen(p.el as unknown as HTMLElement, wide)).resolves.toBe("element");
    expect(p.el.requestFullscreen).toHaveBeenCalledOnce();
    expect(p.orientation.lock).toHaveBeenCalledWith("landscape");
  });
  it("no lock on a desktop, a phone already sideways, or a portrait picture", async () => {
    for (const [o, pic] of [[{ touch: false, portrait: true }, wide], [{ touch: true, portrait: false }, wide], [{ touch: true, portrait: true }, tall]] as const) {
      const p = page(o);
      await expect(fs.enterFullscreen(p.el as unknown as HTMLElement, pic)).resolves.toBe("element");
      expect(p.orientation.lock).not.toHaveBeenCalled();
      vi.unstubAllGlobals();
    }
  });
  it("no picture (a grid of cameras): no lock", async () => {
    const p = page({ touch: true, portrait: true });
    await fs.enterFullscreen(p.el as unknown as HTMLElement, null);
    expect(p.orientation.lock).not.toHaveBeenCalled();
  });
  it("a refused lock (desktop browsers) or no orientation API is swallowed", async () => {
    const p = page({ touch: true, portrait: true, lockFails: true });
    await expect(fs.enterFullscreen(p.el as unknown as HTMLElement, wide)).resolves.toBe("element");
    expect(p.orientation.lock).toHaveBeenCalled();
    p.leave();
    expect(p.orientation.unlock).not.toHaveBeenCalled();   // nothing was locked
    vi.unstubAllGlobals();
    const q = page({ touch: true, portrait: true, noOrientation: true });
    await expect(fs.enterFullscreen(q.el as unknown as HTMLElement, wide)).resolves.toBe("element");
  });
  it("a refused full screen: no lock, nothing thrown", async () => {
    const p = page({ touch: true, portrait: true });
    p.el.requestFullscreen.mockRejectedValueOnce(new TypeError("not allowed"));
    await expect(fs.enterFullscreen(p.el as unknown as HTMLElement, wide)).resolves.toBe("none");
    expect(p.orientation.lock).not.toHaveBeenCalled();
  });
  it("the webkit-prefixed request where that is all there is", async () => {
    const p = page({ touch: true, portrait: true });
    const el = { webkitRequestFullscreen: vi.fn(() => { p.doc.fullscreenElement = el; }) };
    await expect(fs.enterFullscreen(el as unknown as HTMLElement, wide)).resolves.toBe("element");
    expect(el.webkitRequestFullscreen).toHaveBeenCalledOnce();
    expect(p.orientation.lock).toHaveBeenCalledWith("landscape");
  });
  it("an iPhone (no element full screen): the video's own player, no lock", async () => {
    const p = page({ touch: true, portrait: true });
    const v = { videoWidth: 2592, videoHeight: 1520, webkitEnterFullscreen: vi.fn() };
    await expect(fs.enterFullscreen({} as HTMLElement, v as unknown as HTMLVideoElement)).resolves.toBe("video");
    expect(v.webkitEnterFullscreen).toHaveBeenCalledOnce();
    expect(p.orientation.lock).not.toHaveBeenCalled();
  });
});

describe("leaving full screen unlocks", () => {
  it("however it ends (Esc, back): unlocked once, and the listener goes", async () => {
    const p = page({ touch: true, portrait: true });
    await fs.enterFullscreen(p.el as unknown as HTMLElement, wide);
    expect(p.orientation.unlock).not.toHaveBeenCalled();
    p.leave();
    expect(p.orientation.unlock).toHaveBeenCalledOnce();
    p.leave();
    expect(p.orientation.unlock).toHaveBeenCalledOnce();
  });
  it("the exit button: unlocked and out of full screen", async () => {
    const p = page({ touch: true, portrait: true });
    await fs.enterFullscreen(p.el as unknown as HTMLElement, wide);
    fs.exitFullscreen();
    expect(p.orientation.unlock).toHaveBeenCalledOnce();
    expect(p.doc.exitFullscreen).toHaveBeenCalledOnce();
    expect(p.doc.fullscreenElement).toBeNull();
  });
  it("an unlock that throws is swallowed", async () => {
    const p = page({ touch: true, portrait: true, unlockThrows: true });
    await fs.enterFullscreen(p.el as unknown as HTMLElement, wide);
    expect(() => p.leave()).not.toThrow();
    expect(p.orientation.unlock).toHaveBeenCalled();
  });
  it("onFullscreenChange reports the element, and stops when unsubscribed", async () => {
    const p = page();
    const seen: unknown[] = [];
    const off = fs.onFullscreenChange((el) => seen.push(el));
    await fs.enterFullscreen(p.el as unknown as HTMLElement);
    p.leave();
    off();
    await fs.enterFullscreen(p.el as unknown as HTMLElement);
    expect(seen).toEqual([p.el, null]);
  });
});
